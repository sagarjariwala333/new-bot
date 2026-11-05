"""
hub_trading.py
==============

STRATEGY HUB trading for a pair that has a saved plan (pair config `plan`). Mixed into BotInstance
(app/instance.py); a pair WITHOUT a plan never reaches any of this and keeps running the old Base V3 code.

THE FLIP (owner rule, 2026-10-20):
    candle close gives the signal  ->  CLOSE  ->  FLAT (confirmed by the exchange)  ->
    CHECK FOR CHANGES IN RULES  ->  else OPEN THE OPPOSITE AT MARKET AT ONCE (balance read fresh).
    If the rules were changed meanwhile, the NEW rules are applied first and decide what happens next.

Entry wait (plan.entry_wait): 0 = open on the same candle close; N = the signal must still hold on each of
the next N candle closes (strategy_hub.wait_step, same as the hub Pine).

One closed candle is acted on once (hub_state.last_processed_candle). Position state, stop, sizing and
exchange safety checks reuse the same BotInstance helpers as Base V3; this file only holds the hub decisions.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import math
import time
from dataclasses import asdict

import aiohttp
import pandas as pd

from app import base_v3_tracker as tracker_mod
from app import hub_state
from app import ledger
from app import loss_limits
from app import shadow_book
from app import position_state
from app import reconciliation
from app import risk_guard
from app import strategy as strat
from app import strategy_hub as hub
from app import telegram_notifier as tg
from app.email_notifier import email_notifier
from app.exchange_adapter import ExchangeAPIError

HUB_MIN_BARS = 200   # fewer closed candles than this -> wait for more history (indicators need warm-up)

# WAKE AT THE CANDLE CLOSE (owner 2026-10-24: "It should flip instantaneous" / "else we catch running trend").
# The bot wakes right at the close of the candle instead of at its next poll, so the flip (or entry) starts at
# once. Months have no fixed length: "1M" keeps the poll timing.
CANDLE_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}
CLOSE_WAKE_DELAY_S = 0.5     # wake this long after the close, so the exchange already shows the closed candle
CLOSE_CATCHUP_S = 30.0       # the closed candle not seen yet: look again every second for up to this long
# AUDIT FIX A (owner 2026-10-26 "Ok fix all"): a stop-LIMIT on the exchange (Stop Loss / Trailing Stop) does not fill
# when the price jumps past its limit. When the price has gone THROUGH that level and the trade is still open this
# long after, the bot closes at market (the limit order gets this long to fill first).
STOP_LIMIT_GRACE_S = 2.0


class HubMixin:
    # ------------------------------------------------------------------ setup
    def _hub_init(self) -> None:
        """Called at the end of BotInstance.__init__."""
        plan = getattr(self.pc, "plan", None)
        # Deep copy: the running trade must never see a later edit of the stored plan (same rule as the
        # pair-config snapshot isolation).
        # The plan the owner saved needs numbers only for what is switched on; the running copy has the unused
        # settings filled with inert values (never used by any signal or order - see strategy_hub.complete_plan).
        self._plan: dict | None = None
        self._plan_error: str | None = None
        if plan:
            try:
                self._plan = hub.complete_plan(plan)
            except hub.PlanError as e:
                self._plan = copy.deepcopy(plan)
                self._plan_error = str(e)
        self.hub = hub_state.load(self.platform, self.account_id, self.symbol)
        # Shadow mode switched OFF in the plan while a shadow period was running: the period ends here, so a
        # later "ON" never resumes stale paper trades.
        if self._plan is not None and not self._plan_error and not self._plan["shadow"]["on"]:
            sh = self.tracker.get_shadow()
            if sh.active:
                sh.active = False
                sh.pos = 0
                sh.current = None
                sh.last_end_reason = "shadow mode was switched off in the plan"
                self.tracker.set_shadow(sh)
                tracker_mod.save(self.tracker)
        # The wait memory belongs to the plan it was made under: a different plan starts it fresh
        # (the new rules decide, never leftovers of the old ones).
        if self._plan is not None:
            h = self._plan_hash(self._plan)
            if self.hub.plan_hash != h:
                if self.hub.wait.get("pend_dir") or self.hub.shadow_wait.get("pend_dir"):
                    self.hub.wait = {"pend_dir": 0, "pend_bars": 0, "pend_from_flip": False}
                    self.hub.shadow_wait = {"pend_dir": 0, "pend_bars": 0, "pend_from_flip": False}
                self.hub.plan_hash = h
                hub_state.save(self.hub)

    @staticmethod
    def _plan_hash(plan: dict) -> str:
        # The strategy NAME is a label only: renaming it must not count as a change of rules.
        rules = {k: v for k, v in plan.items() if k != "name"}
        return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:16]

    def _is_hub(self) -> bool:
        return self._plan is not None

    @property
    def _hub_busy(self) -> bool:
        """True while a decided order (e.g. the opposite side of a flip) is still waiting to be placed.
        A deferred config restart must not happen in this window."""
        return bool(self._is_hub() and self.hub.open_now)

    def _hub_sizing(self) -> dict:
        return self._plan["sizing"]

    # ------------------------------------------------------------------ data
    async def _hub_fetch(self, plan: dict):
        """Closed candles -> hub frame -> snapshot of the latest closed candle. Pure read."""
        raw = await self.client.get_klines(self.symbol, self.pc.timeframe, limit=hub.HUB_HISTORY_CANDLES + 1)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbbav", "tbqav", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype("int64")
        closed = df.iloc[:-1].reset_index(drop=True)   # the last row is the still-forming candle
        if len(closed) < HUB_MIN_BARS:
            return None, None
        frame = hub.compute_hub(closed, plan)
        snap = hub.last_hub_snapshot(frame)
        self.state.last_candle_time = snap.open_time
        self.state.last_indicators = snap.to_display_dict()
        self.state.updated_at = time.time()
        return frame, snap

    @staticmethod
    def _hub_last_sig_dir(frame) -> int:
        """Last non-zero ENTRY direction BEFORE the latest candle (Pine: lastSigDir)."""
        arr = frame["dir_e"].to_numpy()[:-1]
        nz = arr[arr != 0]
        return int(nz[-1]) if len(nz) else 0

    def _hub_reentry_ok(self, plan: dict, snap, flat: bool, closing_dir: int) -> bool:
        """ "Allow Mid-Trend Re-entry" (owner rule 2026-10-23, OFF by default). OFF: after any close, no new entry
        on the same side while that entry signal is still on. The block ends once the entry signal has ended
        (gone off or turned to the other side); then a new signal on that side may enter again. The other side
        is never blocked. A flip with "Reverse" opens the opposite side, so it is not affected."""
        forced = bool(getattr(self.hub, "reentry_forced", False))    # "Exit when": wait for a fresh signal
        if plan.get("allow_reentry") and not forced:
            if self.hub.reentry_block:
                self.hub.reentry_block = 0
                hub_state.save(self.hub)
            return True
        block = closing_dir or self.hub.reentry_block
        if not block:
            return True
        if flat and not closing_dir and snap.dir_e != block:
            self._log(f"Mid-trend re-entry block on {'LONG' if block == 1 else 'SHORT'} ended "
                      f"(the entry signal is now {('LONG' if snap.dir_e == 1 else 'SHORT') if snap.dir_e else 'off'}).")
            self.hub.reentry_block = 0
            self.hub.reentry_forced = False
            hub_state.save(self.hub)
            return True
        return snap.dir_e != block

    def _hub_first_entry_ok(self, plan: dict, frame, snap) -> bool:
        if not plan["wait_first_reversal"] or self.hub.first_trade_done:
            return True
        last = self._hub_last_sig_dir(frame)
        return snap.dir_e != 0 and last != 0 and snap.dir_e != last

    # ------------------------------------------------------------------ the tick
    async def _tick_hub(self):
        if self._plan_error:
            raise hub.PlanError(self._plan_error)   # refuses a blank / broken plan - no fallback values
        plan = hub.validate_plan(self._plan)
        await self._hub_note_balance()
        frame, snap = await self._hub_fetch(plan)
        if snap is None:
            self._log(f"Waiting for more history (need {HUB_MIN_BARS} closed candles).")
            return

        # The new side of a flip that could not be opened yet (flip close not confirmed flat earlier), or a
        # rules re-check handed over by a restart.
        if self.hub.open_now and self.state.status == "IDLE":
            await self._hub_try_open(snap)
            plan = self._plan   # the rules may have changed inside
            if self.state.status != "IDLE":
                return

        pos = await self.client.get_position_risk(self.symbol)

        if pos is None and self.state.status == "IN_POSITION":
            # Stop (or an outside close) between ticks - finalize, then treat this candle as flat.
            await self._handle_position_closed_externally()
            if self.state.status != "IDLE":
                return

        if pos is not None and self.state.status == "IDLE":
            if not self._foreign_position_alerted:
                self._foreign_position_alerted = True
                msg = (f"A position exists on {self.symbol} that this bot did not open "
                       f"(size {pos.get('positionAmt')}). Real trading on this pair is PAUSED "
                       f"until it is closed. The bot will not touch it.")
                self._log(msg)
                await self._notify(tg.notify_error(self.account_name, self.symbol, msg,
                                                   self.pc.telegram_enabled))
            if self._hub_is_new_candle(snap):
                self._hub_mark_candle(snap)
                await self._hub_shadow_tick(snap)   # paper side keeps its timing
            return
        if pos is None:
            self._foreign_position_alerted = False

        if pos is not None and self.state.status == "IN_POSITION":
            await self._check_position_size_changes(pos)

        if self.state.status == "IN_POSITION":
            await self._hub_manage_tp()   # every tick: take-profit is a touch, not a candle close
        if self.state.status == "IN_POSITION" and pos is not None:
            # every tick (REST price): the backup levels, the Step trailing and the stop on the exchange
            if await self._hub_ladder_check():
                return
        if self._hub_book_open() and self._hub_live_price() is None:
            await self._hub_shadow_live(await self.client.get_mark_price(self.symbol))   # no live feed: REST price

        if not self._hub_is_new_candle(snap):
            return
        self._hub_mark_candle(snap)   # BEFORE acting: a candle is never processed twice
        await self._hub_shadow_tick(snap)   # shadow (paper) block first, then the real orders - like the Pine
        await self._hub_candle(plan, frame, snap, in_position=(pos is not None))

    def _hub_close_wake_in(self, now_s: float | None = None) -> float | None:
        """Seconds until the bot should wake for the next candle close, or None (= keep the poll timing).
        The candle after the last processed one closes at last_open + 2 x candle length (the exchange's own open
        times, so any alignment the exchange uses is kept). Once that time has passed and the candle is still not
        processed (the exchange was a little late), the bot looks again every second for up to 30 seconds."""
        tf = CANDLE_MS.get(getattr(self.pc, "timeframe", None) or "")
        last = self.hub.last_processed_candle if getattr(self, "hub", None) is not None else None
        if not tf or last is None:
            return None
        now_ms = (time.time() if now_s is None else now_s) * 1000.0
        close_ms = int(last) + 2 * tf
        if now_ms < close_ms:
            return (close_ms - now_ms) / 1000.0 + CLOSE_WAKE_DELAY_S
        if now_ms - close_ms <= CLOSE_CATCHUP_S * 1000.0:
            return 1.0
        return None

    def _hub_is_new_candle(self, snap) -> bool:
        last = self.hub.last_processed_candle
        return last is None or snap.open_time > last

    def _hub_mark_candle(self, snap) -> None:
        self.hub.last_processed_candle = snap.open_time
        hub_state.save(self.hub)

    async def _hub_continue(self):
        """Right after a flip close that needed more than one try: open the decided side as soon as flat."""
        plan = hub.validate_plan(self._plan)
        frame, snap = await self._hub_fetch(plan)
        if snap is not None:
            await self._hub_try_open(snap)

    # ------------------------------------------------------------------ one closed candle
    async def _hub_candle(self, plan: dict, frame, snap, in_position: bool):
        reverse = plan["flip_action"] == hub.FLIP_ACTIONS[0]
        ws = hub.WaitState(**self.hub.wait)
        flipped_to = 0
        direction = self.state.direction

        if in_position and self.state.status == "IN_POSITION" and direction:
            decision = hub.flip_decision(direction, snap, self.state.entry_price, plan)
            if decision == "HOLD":
                self.state.held_signal_count += 1
                self.state.last_decision = f"HOLD - flip signal ignored (in profit, ADX {snap.adx:.1f} below the hold level)"
                self._log(f"Flip signal while {direction} - HELD by the Hold Rule "
                          f"(close {snap.close} vs entry {self.state.entry_price}, ADX {snap.adx:.2f}).")
            elif decision == "FLIP":
                flipped_to = snap.dir_f
                self.state.last_decision = f"FLIP {direction} - closing now"
                self._log(f"Flip signal while {direction} on the candle close - CLOSE, then FLAT, then "
                          f"check the rules, then {'open the opposite' if reverse else 'stay flat (close only)'}.")
            else:
                self.state.last_decision = f"IN {direction} - no flip signal"
            # EXIT WHEN (optional): the MA(s) curve against the trade at this candle close. A flip that fires on the
            # same candle is done first (it already closes the trade).
            if decision != "FLIP" and hub.exit_decision(direction, snap, plan) == "EXIT":
                if hub.exit_after(plan) == hub.EXIT_FLIP:
                    flipped_to = -1 if direction == "LONG" else 1
                    self.state.last_decision = f"EXIT WHEN {direction} - closing now, then the other side"
                    self._log(f"Exit when: the MA(s) curve against the {direction} on the candle close - CLOSE, then "
                              f"FLAT, then check the rules, then open the opposite.")
                else:
                    self.state.last_decision = f"EXIT WHEN {direction} - closing now, then flat until a fresh signal"
                    self._log(f"Exit when: the MA(s) curve against the {direction} on the candle close - CLOSE and stay "
                              f"flat until a fresh entry signal.")
                    await self._close_position(reason="exit_when")
                    return
            # SUPPORT / RESISTANCE exits (optional): the close of this candle meets a ticked exit. Always "close and wait
            # for a fresh signal". A flip or Exit when that fires on the same candle is done first.
            if decision != "FLIP" and flipped_to == 0 and hub.sr_exit_decision(
                    direction, snap, plan, int(getattr(self.hub, "sr_origin", 0) or 0)) == "EXIT":
                self.state.last_decision = f"SR EXIT {direction} - closing now, then flat until a fresh signal"
                self._log(f"Support / Resistance exit: the candle close meets a ticked exit - CLOSE the {direction} and "
                          f"stay flat until a fresh entry signal.")
                await self._close_position(reason="sr_exit")
                return
        elif in_position:
            return   # a position is open but the bot is not in the normal IN_POSITION state (closing / unprotected)

        if in_position and self.state.status == "IN_POSITION" and flipped_to == 0:
            if await self._hub_trail(snap):
                return   # the trailing level was already crossed: closed; the next candle is judged fresh
            if await self._hub_time_stop(snap):
                return   # TIME STOP closed the trade; the next candle is judged fresh

        flat = (not in_position) or flipped_to != 0
        # A flip close blocks its own side on this candle too (the close happens just below).
        closing_dir = (1 if direction == "LONG" else -1) if (flipped_to != 0 and direction) else 0
        reentry_ok = self._hub_reentry_ok(plan, snap, flat, closing_dir)
        ready = hub.wait_step(ws, flipped_to=flipped_to, reverse=reverse, flat=flat, dir_e=snap.dir_e,
                              dir_f=snap.dir_f,
                              first_entry_ok=self._hub_first_entry_ok(plan, frame, snap) and reentry_ok,
                              entry_wait=plan["entry_wait"], flip_wait=hub.flip_wait(plan))
        self.hub.wait = asdict(ws)
        if ready != 0:
            self.hub.open_now = {"dir": int(ready), "candle": int(snap.open_time), "rules_check": False}
        hub_state.save(self.hub)

        if flipped_to != 0:
            await self._close_position(reason="signal_flip")   # confirms FLAT with the exchange
        elif not in_position and ready == 0:
            if snap.dir_e == 0:
                self.state.last_decision = "NO SIGNAL"
            elif ws.pend_dir != 0:
                self.state.last_decision = f"WAITING entry wait ({ws.pend_bars}/{plan['entry_wait']} candles)"
            elif not reentry_ok:
                self.state.last_decision = ("NO ENTRY - mid-trend re-entry is not allowed (waiting for this "
                                            "entry signal to end)")
            else:
                self.state.last_decision = "NO ENTRY (blocked by a rule)"
        if self.hub.open_now:
            await self._hub_try_open(snap)

    # ------------------------------------------------------------------ CHECK FOR CHANGES IN RULES, then open
    async def _hub_try_open(self, snap):
        """Flat -> (rules check) -> open the decided side at market. Does nothing unless flat."""
        on = self.hub.open_now
        if not on or self.state.status != "IDLE":
            return

        # Decisions belong to ONE candle; an older one is dropped (the bot looks at the new candle fresh).
        if on.get("candle") != snap.open_time and not on.get("rules_check"):
            self._log("A decided entry belongs to an older candle - dropped; the new candle is judged fresh.")
            self.hub.open_now = None
            hub_state.save(self.hub)
            return

        # ---- CHECK FOR CHANGES IN RULES (first thing once flat, nothing awaited before it) ----
        if self._pending_restart_on_flat or on.get("rules_check"):
            applied = await self._hub_apply_rule_change(snap)
            if not applied:
                return   # handed to the restart; the new instance re-judges under the new rules
            on = self.hub.open_now
            snap = self._hub_after_rules_snap
            if not on:
                return

        if self._plan["shadow"]["on"]:
            shadow = self.tracker.get_shadow()
            if not strat.real_entry_allowed(shadow, snap.open_time):
                why = (f"SHADOW MODE is on ({shadow.count}/{self._plan['shadow']['trades']} paper trades done)"
                       if shadow.active else "shadow mode just ended on this candle")
                self._log(f"Not opening a real trade: {why}. Real entries resume when it ends.")
                self.state.last_decision = "SHADOW MODE - paper trades only, no real entry"
                self.hub.open_now = None
                hub_state.save(self.hub)
                return

        # COOLDOWN AFTER A LOSS and LOSS LIMIT: they only stop a NEW entry. An open trade is never touched.
        if not await self._hub_entry_gates(snap):
            return

        direction = "LONG" if on["dir"] == 1 else "SHORT"
        self.state.last_decision = f"OPEN {direction} (flat confirmed, rules unchanged)"
        result = await self._hub_maybe_enter(direction, snap)
        if result in ("opened", "dropped"):
            self.hub.open_now = None
            hub_state.save(self.hub)

    async def _hub_note_balance(self):
        """Only when a Loss limit in % applies to this account: saves the account balance once at the start of every day
        and week (the base of the %). Read only, never touches a trade, and a failure here never stops the tick."""
        try:
            if not loss_limits.balance_due(self.account_id, self._plan):
                return
            loss_limits.record_balance(self.account_id, await self.client.get_equity())
        except Exception as e:
            now = time.time()
            if now - getattr(self, "_hub_bal_err_at", 0.0) > 600:     # at most one line every 10 minutes
                self._hub_bal_err_at = now
                self._log(f"Loss limit: could not save the account balance for the % limit ({e}). Tried again next tick.")

    async def _hub_entry_gates(self, snap) -> bool:
        """True = the entry may go ahead. False = a Cooldown after a loss or a Loss limit stops it: the decision for
        this candle is dropped (the next candle is judged fresh) and the reason is logged and shown as the decision."""
        cd = hub.cooldown_cfg(self._plan)
        if cd is not None and hub.cooldown_blocks(self.hub.cooldown_through, snap.open_time):
            tf_ms = CANDLE_MS.get(getattr(self.pc, "timeframe", None) or "")
            left = hub.cooldown_candles_left(self.hub.cooldown_through, snap.open_time, tf_ms)
            msg = (f"COOLDOWN after a loss - no new entry on this candle close ({left} candle close(s) left "
                   f"including this one, {self.hub.loss_streak} loss(es) in a row).")
            self._log(msg)
            self.state.last_decision = msg
            self.hub.open_now = None
            hub_state.save(self.hub)
            return False
        try:
            equity = None
            if loss_limits.needs_balance(self.account_id, self._plan):     # a % limit: its base is the account balance
                try:
                    equity = await self.client.get_equity()
                except Exception as e:
                    self._log(f"Loss limit: the account balance could not be read ({e}). A limit in % is checked "
                              f"only if the balance at the start of the period was saved earlier; USDT limits are checked.")
            hit = loss_limits.check(self.account_id, self.symbol, self._plan, equity=equity)
        except Exception as e:
            # A broken check must not stop trading by surprise, and must not stay quiet either.
            self._log(f"Loss limit check failed ({e}) - the entry goes ahead. Look at the loss limits.")
            return True
        if hit is None:
            return True
        self._log(hit["text"])
        self.state.last_decision = hit["text"]
        self.hub.open_now = None
        hub_state.save(self.hub)
        if loss_limits.first_time(hit["key"]):
            try:
                await self._notify(tg.notify_info(self.account_name, self.symbol, hit["text"], self.pc.telegram_enabled,
                                                  email_kind="Loss Limit"))
                # With Telegram on but not set up, the Telegram sender already sends the email: not twice.
                if not (self.pc.telegram_enabled and not tg.notifier.configured):
                    await email_notifier.send(
                        "Trade-Bot - Loss Limit",
                        f"Account: {self.account_name}\nSymbol: {self.symbol}\n{hit['text']}")
            except Exception as e:
                self._log(f"Loss limit message could not be sent ({e}) - the limit still holds.")
        return False

    async def _hub_apply_rule_change(self, snap) -> bool:
        """The pair is flat and a config change is waiting (or a restart handed the check over).
        True  = the new rules are loaded here; self.hub.open_now now holds what the NEW rules decide.
        False = something that needs a real restart changed (timeframe etc.): the restart is left to the
                manager and the new instance judges the candle under the new rules."""
        self._hub_after_rules_snap = snap
        store = getattr(self.manager, "store", None)
        new_pc = None
        if store is not None:
            acc = store.get_account(self.account_id)
            stored = acc.pairs.get(self.symbol) if acc is not None else None
            new_pc = dataclasses.replace(stored) if stored is not None else None

        if new_pc is None:
            # Nothing to compare with: judge the candle again under the rules this instance runs.
            return await self._hub_rejudge(snap, "rules re-checked")

        in_process_ok = (
            bool(getattr(new_pc, "plan", None))
            and new_pc.timeframe == self.pc.timeframe
            and new_pc.isolated_margin == self.pc.isolated_margin
            and new_pc.ws_staleness_seconds == self.pc.ws_staleness_seconds
            and getattr(new_pc, "trigger_px_type", None) == getattr(self.pc, "trigger_px_type", None)
        )
        if not in_process_ok:
            if self.hub.open_now:
                self.hub.open_now["rules_check"] = True
                hub_state.save(self.hub)
            self._log("Rules changed in a way that needs a restart - the restart applies them and the new "
                      "rules decide what happens next.")
            return False

        try:
            hub.validate_plan(new_pc.plan)
        except hub.PlanError as e:
            self._log(f"The changed plan is not valid ({e}) - NOT opening. Fix the plan; the bot stays flat.")
            self.hub.open_now = None
            hub_state.save(self.hub)
            self._pending_restart_on_flat = False
            return False

        self.pc = new_pc
        self._plan = hub.complete_plan(new_pc.plan)
        self._pending_restart_on_flat = False
        self._log("Rules changed during the flip - NEW rules applied first (" + hub.plan_summary(self._plan) + ").")
        return await self._hub_rejudge(snap, "rules changed during the flip")

    async def _hub_rejudge(self, snap, why: str) -> bool:
        """Judge the latest candle again under the CURRENT plan as a fresh, flat situation."""
        plan = hub.validate_plan(self._plan)
        frame, new_snap = await self._hub_fetch(plan)
        if new_snap is None:
            self.hub.open_now = None
            hub_state.save(self.hub)
            return False
        ws = hub.WaitState()
        reverse = plan["flip_action"] == hub.FLIP_ACTIONS[0]
        reentry_ok = self._hub_reentry_ok(plan, new_snap, True, 0)
        ready = hub.wait_step(ws, flipped_to=0, reverse=reverse, flat=True, dir_e=new_snap.dir_e,
                              dir_f=new_snap.dir_f,
                              first_entry_ok=self._hub_first_entry_ok(plan, frame, new_snap) and reentry_ok,
                              entry_wait=plan["entry_wait"], flip_wait=hub.flip_wait(plan))
        self.hub.wait = asdict(ws)
        self.hub.plan_hash = self._plan_hash(self._plan)
        self.hub.last_processed_candle = new_snap.open_time
        self.hub.open_now = ({"dir": int(ready), "candle": int(new_snap.open_time), "rules_check": False}
                             if ready != 0 else None)
        hub_state.save(self.hub)
        self._hub_after_rules_snap = new_snap
        self._log(f"New rules judged the candle ({why}): "
                  f"{'open ' + ('LONG' if ready == 1 else 'SHORT') if ready else 'no entry now'}.")
        return True

    # ------------------------------------------------------------------ entry (same safety checks as Base V3)
    async def _hub_maybe_enter(self, direction: str, snap) -> str:
        if self.manager is not None:
            async with self.manager.get_account_lock(self.account_id):
                return await self._hub_do_enter(direction, snap)
        return await self._hub_do_enter(direction, snap)

    async def _hub_do_enter(self, direction: str, snap) -> str:
        """Returns "opened", "dropped" (will not be retried) or "retry" (try again next tick, same candle)."""
        sz = self._hub_sizing()

        if reconciliation.has_pending_reconciliation(self.account_id, self.symbol):
            try:
                pos = await self.client.get_position_risk(self.symbol)
                self._pending_reconciliation_query_failures.record_success()
            except Exception as e:
                self._log(f"Pending reconciliation exists and the exchange couldn't be reached to resolve it "
                          f"({e}) - refusing to open a new position until this is confirmed.")
                if self._pending_reconciliation_query_failures.record_failure():
                    await self._notify(tg.notify_error(
                        self.account_name, self.symbol,
                        f"Could not resolve a pending-reconciliation flag {self._pending_reconciliation_query_failures.threshold} "
                        f"times in a row ({e}). New entries stay blocked on this symbol.", self.pc.telegram_enabled))
                return "retry"
            if pos is not None:
                self._log("Pending reconciliation exists AND the exchange shows an open position - refusing to "
                          "open a new one on top of it. This needs a manual check.")
                return "dropped"
            self._log("Pending reconciliation resolved: exchange confirms flat. Clearing the flag.")
            reconciliation.clear_pending_reconciliation(self.account_id, self.symbol)

        self.state.feed_stale = self.mark_feed.is_stale()
        if self.state.feed_stale:
            self._log("Mark-price feed is stale - not opening yet; will retry while this candle is current.")
            return "retry"

        # Never open on top of anything that is already there.
        existing = await self.client.get_position_risk(self.symbol)
        if existing is not None:
            self._log("A position is already open on this symbol - not opening another (no stacking).")
            return "dropped"

        # READ THE BALANCE now (after the close): this one number sizes the trade and the max-loss stop.
        available_balance = await self.client.get_available_balance()
        alloc, lev, size_kind = hub.entry_size(direction, snap, self._plan)
        if hub.counter_entry_off(size_kind, self._plan):
            # owner 2026-10-25: counter-trend size 0 = no counter-trend entry at all (a flip closes and stays flat)
            self._log(f"Not opening {direction}: it is a COUNTER-TREND entry and the counter-trend size is 0 "
                      f"(no counter-trend entries). Waiting for the next signal.")
            self.state.last_decision = f"NO ENTRY - counter-trend {direction} (counter-trend size 0)"
            return "dropped"
        fixed = hub.uses_fixed_base(self._plan)
        entry_amount = None
        if fixed:
            # FIXED BASE AMOUNT: the margin is the total of the last closed trade (never below the base);
            # the position is that margin times the leverage.
            entry_amount = hub.fixed_entry_amount(size_kind, self._plan, self.hub.last_total)
            last = self.hub.last_total
            self._log(f"Entry size: FIXED amount {entry_amount:,.4f} USDT at {lev:g}x "
                      f"({size_kind.upper()} entry; last closed total "
                      f"{('%.4f' % last) if last is not None else 'none yet'}).")
            raw_qty = (entry_amount * lev / snap.close) if snap.close > 0 else 0.0
            alloc = (entry_amount / available_balance * 100.0) if available_balance > 0 else 0.0
        else:
            if size_kind != "single":
                by = (self._plan["sizing"].get("counter") or {}).get("by", hub.CT_EMA)
                why = (f"chart close {snap.c_close} vs sizing EMA {snap.size_ema:.2f}" if by == hub.CT_EMA
                       else "decided by the Bollinger half (the middle line)" if by == "bb"
                       else f"decided by {hub.BLOCK_TITLES.get(by, by)}")
                self._log(f"Entry size: {size_kind.upper()} trade ({why}) -> {alloc}% at {lev:g}x.")
            raw_qty = strat.position_qty(available_balance, alloc, lev, snap.close)
        qty = await self.client.round_qty(self.symbol, raw_qty)
        if qty <= 0:
            self._log(f"Computed qty rounded to 0 (available balance {available_balance}, allocation "
                      f"{alloc}%, close {snap.close}) - skipping entry.")
            return "dropped"

        await self._hub_margin_alert(qty, snap.close, lev, available_balance, snap.open_time)

        notional_ok, notional_msg = await self.client.check_min_notional(self.symbol, qty, snap.close)
        if not notional_ok:
            self._log(f"Skipping entry: {notional_msg}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, notional_msg, self.pc.telegram_enabled))
            return "dropped"

        try:
            brackets = await self.client.get_leverage_brackets(self.symbol)
            self._bracket_fetch_failures.record_success()
        except Exception as e:
            self._log(f"Could not fetch leverage brackets ({e}) - skipping this check; the exchange still enforces its limits.")
            brackets = []
            if self._bracket_fetch_failures.record_failure():
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    f"Leverage-bracket check has failed {self._bracket_fetch_failures.threshold} times in a row ({e}).",
                    self.pc.telegram_enabled))
        from app.instance import _leverage_fits_bracket
        bracket_ok, bracket_msg = _leverage_fits_bracket(brackets, qty * snap.close, lev)
        if not bracket_ok:
            self._log(f"Skipping entry: {bracket_msg}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, bracket_msg, self.pc.telegram_enabled))
            return "dropped"

        equity = await self.client.get_equity()
        if self.manager is not None:
            acc = self.manager.store.get_account(self.account_id)
            cap = acc.max_account_exposure_pct if acc else None
            if cap:
                try:
                    real_positions = await self.client.get_all_open_positions()
                except Exception as e:
                    self._log(f"Could not fetch real positions for the exposure check ({e}) - not opening now.")
                    return "retry"
                allowed, exp_msg = risk_guard.check_new_entry_allowed(
                    real_positions, exclude_symbol=self.symbol, new_notional=qty * snap.close,
                    equity=equity, max_account_exposure_pct=cap)
                if not allowed:
                    self._log(f"Entry skipped: {exp_msg}")
                    await self._notify(tg.notify_error(self.account_name, self.symbol, exp_msg, self.pc.telegram_enabled))
                    return "dropped"

        side = "BUY" if direction == "LONG" else "SELL"
        try:
            await self.client.cancel_all_open_orders(self.symbol)
        except ExchangeAPIError as e:
            self._log(f"Pre-entry cleanup (cancel all orders) failed: {e} - verifying the symbol is clean.")
            try:
                regular_orders = await self.client.get_open_orders(self.symbol)
                algo_orders = await self.client.get_open_algo_orders(self.symbol)
            except Exception as e2:
                self._log(f"Could not verify the symbol is clean either ({e2}) - not opening now.")
                return "retry"
            if regular_orders or algo_orders:
                self._log(f"Cleanup failed AND {len(regular_orders) + len(algo_orders)} order(s) are still resting "
                          f"on {self.symbol} - not opening.")
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    "Entry skipped: pre-entry cleanup failed and orders are still confirmed resting.",
                    self.pc.telegram_enabled))
                return "dropped"
            self._log("Verified the symbol is actually clean despite the cleanup error - proceeding.")

        try:
            # AUDIT FIX C: the margin mode (Isolated / Cross) of the pair is set at every entry (never while open)
            await self.client.set_margin_type(self.symbol, self.pc.isolated_margin)
        except Exception as e:
            mm_msg = (f"Entry skipped: could not set the margin mode to "
                      f"{'Isolated' if self.pc.isolated_margin else 'Cross'} ({e}). Not entering in an unconfirmed "
                      f"margin mode.")
            self._log(mm_msg)
            await self._notify(tg.notify_error(self.account_name, self.symbol, mm_msg, self.pc.telegram_enabled))
            return "retry"
        try:
            await self.client.set_leverage(self.symbol, int(lev))
        except Exception as e:
            lev_msg = (f"Entry skipped: could not set exchange leverage to {int(lev)}x ({e}). "
                       f"Not entering at an unconfirmed leverage.")
            self._log(lev_msg)
            await self._notify(tg.notify_error(self.account_name, self.symbol, lev_msg, self.pc.telegram_enabled))
            return "retry"

        try:
            order = await self.client.market_order(self.symbol, side, qty)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await self._flag_and_notify_pending_reconciliation(
                context="market entry", client_order_id=getattr(e, "client_order_id", "unknown"), detail=str(e))
            self.hub.open_now = None   # no blind retry of an order whose outcome is unknown
            hub_state.save(self.hub)
            raise
        # The order is placed: this decision is used up.
        self.hub.open_now = None
        self.hub.first_trade_done = True
        # Fixed base amount: remember this trade's entry amount; every fill of the trade is added to trade_pnl
        # and the trade's total becomes last_total when it is fully closed (_hub_trade_finished).
        self.hub.trade_amount = entry_amount
        self.hub.trade_pnl = 0.0
        self.hub.res_open = True      # Cooldown after a loss: this trade's fills are added up from now on
        self.hub.res_pnl = 0.0
        self.hub.outside_closed_qty = 0.0
        self.hub.outside_fill_ids = []
        hub_state.save(self.hub)

        entry_price = float(order.get("avgPrice") or 0) or None
        entry_order_id = order.get("orderId")
        if entry_price is None:
            try:
                recent_trades = await self.client.get_user_trades(self.symbol, order_id=entry_order_id, limit=5)
                if recent_trades:
                    entry_price = float(recent_trades[-1].get("price") or 0) or None
            except Exception as e:
                self._log(f"Could not recover the real fill price after entry ({e}) - using a mark-price estimate.")
            if entry_price is None:
                try:
                    entry_price = await self.client.get_mark_price(self.symbol)
                except Exception:
                    entry_price = snap.close
                self._log(f"Order response had no fill price - recovered {entry_price} as an estimate.")
        self._clear_pending_reconciliation_on_success(order.get("clientOrderId"))

        try:
            real_pos = await self.client.get_position_risk(self.symbol)
            if real_pos is not None:
                real_qty = abs(float(real_pos["positionAmt"]))
                if abs(real_qty - qty) > 1e-9:
                    self._log(f"Real filled quantity ({real_qty}) differs from the calculated size ({qty}) - "
                              f"using the real exchange quantity.")
                qty = real_qty
            else:
                self._log("Position query right after entry shows flat - unexpected. Using the calculated quantity.")
        except Exception as e:
            self._log(f"Could not confirm the real filled quantity ({e}) - using the calculated size ({qty}).")

        # STOP LADDER (2026-10-23): Max Loss, Stop Loss and Trailing Stop levels of this trade.
        ladder = self._hub_new_ladder(direction, entry_price, qty, lev, available_balance, snap)
        _, requested_stop = hub.nearest_level(direction, ladder)
        if ladder["sl"] is None:
            await self._hub_sl_missing_alert(direction, entry_price)

        self.state.direction = direction
        self.state.entry_price = entry_price
        self.state.qty = qty
        self.state.bot_qty = qty
        self.state.entry_order_id = entry_order_id
        self.state.opened_at = time.time()
        self.state.equity_at_entry = available_balance
        self.state.stop_target = ladder["ml"] if ladder["ml"] is not None else requested_stop
        self.state.entry_atr = snap.tp_atr if snap.tp_atr == snap.tp_atr else None
        self.state.tp1_price = self.state.tp2_price = None
        self.state.tp1_touched = self.state.tp2_touched = False
        self.state.alloc_pct = alloc
        self.state.leverage = lev
        self.state.entry_candle_time = snap.open_time
        self.state.held_signal_count = 0
        self._topup_alerted = False
        self._persist_trade_state()
        self.hub.ladder = ladder
        # Support / Resistance: how this trade was entered, so "Support / Resistance trades only" exits know the rule
        sr_side = bool(getattr(snap, "sr_long" if direction == "LONG" else "sr_short", False))
        self.hub.sr_origin = int(getattr(snap, "sr_origin", 0) or 0) if sr_side else 0
        hub_state.save(self.hub)

        if requested_stop is None:
            self.state.status = "IN_POSITION"
            self.state.sl_price = None
            self.state.sl_order_id = None
            why = ("the Trailing Stop takes charge at its first checkpoint" if self._plan["trail"]["on"]
                   else (hub.SL_NOT_WORKED_MSG if self._plan["sl"]["on"] else hub.SL_MISSING_MSG).rstrip("."))
            self.state.last_decision = f"ENTERED {direction} (no stop yet - {why})"
            self._persist_trade_state()
            self._log(f"Opened {direction} {qty} @ {entry_price} | balance {available_balance:.2f} | "
                      f"{('amount %.4f USDT' % entry_amount) if fixed else ('allocation %s%%' % alloc)} | "
                      f"NO stop yet ({why})")
            await self._notify(tg.notify_entry(self.account_name, self.symbol, direction, qty, entry_price,
                                               None, None, None, lev, self.pc.telegram_enabled,
                                               alloc_pct=None if fixed else alloc))
            await self._hub_after_entry(direction, entry_price, qty, snap)
            return "opened"

        # The nearest level goes on the exchange now (stop-limit for Stop Loss / Trailing Stop, stop-market for
        # Max Loss); the levels behind it are watched by the bot. Same safety as before: one retry, then the
        # unprotected path (protect again or flatten) - a position is never left without its stop.
        self.state.status = "IN_POSITION"
        self.state.sl_price = None
        self.state.sl_order_id = None
        try:
            closed = await self._hub_sync_exchange_stop(None, first=True)
        except Exception as e1:
            self._log(f"Stop placement failed right after entry ({e1}) - retrying once immediately.")
            await asyncio.sleep(2)
            try:
                closed = await self._hub_sync_exchange_stop(None, first=True)
            except Exception as e2:
                await self._enter_unprotected_state(direction, qty, entry_price,
                                                    reason=f"stop placement failed twice after entry: {e2}")
                return "opened"
        if closed:
            return "opened"   # the price was already through the stop level: closed at market

        sl_price = self.state.sl_price
        ex = (self.hub.ladder or {}).get("ex") or {}
        self.state.last_decision = f"ENTERED {direction}"
        self._persist_trade_state()
        self._log(f"Opened {direction} {qty} @ {entry_price} | balance {available_balance:.2f} | "
                  f"{('amount %.4f USDT' % entry_amount) if fixed else ('allocation %s%%' % alloc)} | "
                  f"{self._hub_ladder_text()} | on the exchange: {hub.LADDER_NAMES.get(ex.get('name'), '?')} "
                  f"{sl_price} ({'stop-limit' if ex.get('kind') == 'limit' else 'stop-market'})")
        await self._notify(tg.notify_entry(self.account_name, self.symbol, direction, qty, entry_price,
                                           sl_price, None, None, lev, self.pc.telegram_enabled,
                                           alloc_pct=None if fixed else alloc))
        await self._hub_after_entry(direction, entry_price, qty, snap)
        return "opened"

    async def _hub_sl_missing_alert(self, direction: str, entry_price) -> None:
        """owner 2026-10-26: no SL set, or the SL price could not be worked out at entry -> the trade stays open and the
        owner is told (Telegram + email) to set it manually. An error here can never break trading."""
        try:
            slp = self._plan.get("sl") or {}
            if slp.get("on"):
                msg = f"{hub.SL_NOT_WORKED_MSG} ({direction} @ {entry_price}, Stop Loss measured on: {slp.get('basis')})"
            else:
                msg = f"{hub.SL_MISSING_MSG} ({direction} @ {entry_price})"
            self._log(msg)
            await self._notify(tg.notify_info(self.account_name, self.symbol, msg, self.pc.telegram_enabled,
                                              email_kind="SL Alert"))
            await email_notifier.send("Trade-Bot - SL Alert",
                                      f"Account: {self.account_name}\nSymbol: {self.symbol}\n{msg}")
        except Exception as e:
            self._log(f"SL alert could not be sent ({e}).")

    async def _hub_time_stop(self, snap) -> bool:
        """TIME STOP (owner 2026-10-26): at the candle close, the trade has been open N candles and is not in profit
        -> close it. Candles are counted from the entry candle. Returns True if it closed the trade."""
        sl = self._plan.get("sl") or {}
        if not (sl.get("on") and sl.get("time_on")):
            return False
        d, entry, t0 = self.state.direction, self.state.entry_price, self.state.entry_candle_time
        tf = CANDLE_MS.get(getattr(self.pc, "timeframe", None) or "")
        if not d or not entry or t0 is None or not tf:
            return False
        bars = int((int(snap.open_time) - int(t0)) // tf)
        if not hub.time_stop_due(d, self._plan, bars, snap.close, float(entry)):
            return False
        self._log(f"TIME STOP: open {bars} candles and not in profit (close {snap.close} vs entry {entry}) - closing.")
        self.state.last_decision = f"TIME STOP - {d} closed (open {bars} candles, not in profit)"
        await self._close_position(reason="time_stop")
        return True

    async def _hub_margin_alert(self, qty: float, price: float, lev, available_balance: float, candle_time) -> None:
        """Box "Email alert on margin" (sizing.margin_alert). When the margin this entry needs is more than the
        available balance right now (other pairs on the account have already taken part of it), send an email
        (and the Telegram message). Once per candle. It never stops or changes the entry itself, and an error
        here can never break trading."""
        try:
            if not self._plan["sizing"].get("margin_alert"):
                return
            margin = qty * price / float(lev or 1)
            if margin <= available_balance:
                return
            if getattr(self, "_margin_alert_candle", None) == candle_time:
                return
            self._margin_alert_candle = candle_time
            msg = (f"Margin alert: this entry needs {margin:,.2f} USDT margin ({qty * price:,.2f} USDT position at "
                   f"{float(lev):g}x) but the available balance is {available_balance:,.2f} USDT. "
                   f"The margins of all pairs together would be more than the available balance.")
            self._log(msg)
            await self._notify(tg.notify_info(self.account_name, self.symbol, msg, self.pc.telegram_enabled,
                                              email_kind="Margin Alert"))
            await email_notifier.send(
                "Trade-Bot - Margin Alert",
                f"Account: {self.account_name}\nSymbol: {self.symbol}\n{msg}")
        except Exception as e:
            self._log(f"Margin alert could not be sent ({e}) - trading continues.")

    def _hub_stop_target(self, direction: str, entry_price: float, qty: float, leverage, available_balance: float) -> float:
        """The max-loss stop price, by the MAX LOSS basis of the plan (% of available balance, % of the entry
        amount, or a fixed $ amount). The entry amount is the real margin: quantity x entry price / leverage."""
        margin = qty * entry_price / float(leverage or 1)
        loss = hub.max_loss_usd(self._plan, available_balance, margin)
        return hub.stop_price_from_usd(direction, entry_price, qty, loss)

    def _hub_max_loss_text(self) -> str:
        sz = self._plan["sizing"]
        basis = sz.get("stop_basis", hub.STOP_BAL)
        if basis == hub.STOP_USD:
            return f"{sz['stop_usd']} USDT"
        return f"{sz['stop_pct']}% of the " + ("entry amount" if basis == hub.STOP_ENTRY else "balance")

    def _hub_note_result(self, net_pnl) -> None:
        """Add one fill of the open trade (net of fees) to the trade's result (Cooldown after a loss) and, for the
        fixed base amount, to the trade's total."""
        if not self._is_hub():
            return
        if self.hub.res_open and net_pnl is not None:
            self.hub.res_pnl = float(self.hub.res_pnl or 0.0) + float(net_pnl)
            hub_state.save(self.hub)
        if self.hub.trade_amount is None:
            return
        if net_pnl is None:
            self._log("A fill of this trade has no known result (no exit price) - counted as 0 in the trade total.")
            return
        self.hub.trade_pnl = float(self.hub.trade_pnl or 0.0) + float(net_pnl)
        hub_state.save(self.hub)

    def _hub_result_finished(self) -> None:
        """The trade is fully closed: its result (all fills added up, net of fees) ends or extends the losses in a
        row, and a Cooldown after a loss starts when the plan asks for it (see hub.cooldown_after_close)."""
        if not self._is_hub() or not self.hub.res_open:
            return
        result = float(self.hub.res_pnl or 0.0)
        tf_ms = CANDLE_MS.get(getattr(self.pc, "timeframe", None) or "")
        streak, through = hub.cooldown_after_close(self._plan, self.hub.loss_streak, result,
                                                   self.hub.last_processed_candle, tf_ms)
        self.hub.loss_streak = streak
        self.hub.cooldown_through = through
        self.hub.res_open = False
        self.hub.res_pnl = 0.0
        hub_state.save(self.hub)
        cd = hub.cooldown_cfg(self._plan)
        if cd is None:
            return
        if through is not None:
            self._log(f"Trade closed with a result of {result:+,.4f} USDT ({streak} loss(es) in a row). Cooldown after a loss: "
                      f"no new entry for {int(cd['candles'])} candle close(s).")
        elif result < 0:
            self._log(f"Trade closed with a result of {result:+,.4f} USDT ({streak} loss(es) in a row). Cooldown starts after "
                      f"{int(cd.get('after_losses') or 1)} loss(es) in a row.")

    def _hub_trade_finished(self) -> None:
        """Fixed base amount: the trade is fully closed. Its total (entry amount + all fills added up) is what the
        next entry is built on (never below the base, see hub.fixed_entry_amount)."""
        if not self._is_hub() or self.hub.trade_amount is None:
            return
        lock = self._plan["sizing"].get("lock_pct")
        total = hub.carry_total(self.hub.trade_amount, self.hub.trade_pnl, lock)
        if lock is None:
            self._log(f"Trade total: entry amount {self.hub.trade_amount:,.4f} + result {self.hub.trade_pnl:+,.4f} "
                      f"= {total:,.4f} USDT. The next entry is built on this total (never below the base).")
        else:
            self._log(f"Trade total with lock-out {lock:g}%: entry amount {self.hub.trade_amount:,.4f}, result "
                      f"{self.hub.trade_pnl:+,.4f} (a profit adds {lock:g}% of it, a loss comes off in full) "
                      f"= {total:,.4f} USDT. The next entry is built on this (never below the base).")
        self.hub.last_total = total
        self.hub.trade_amount = None
        self.hub.trade_pnl = 0.0
        hub_state.save(self.hub)

    async def _hub_after_entry(self, direction: str, entry_price: float, qty: float, snap) -> None:
        """Take profit: remember all levels now (persisted), place TP1 at once. Trailing needs nothing here."""
        plan = self._plan
        self.hub.tp = None
        if plan["tp"]["on"]:
            atr = snap.tp_atr
            if plan["tp"].get("basis", hub.BASIS_ATR) == hub.BASIS_ATR and (not math.isfinite(atr) or atr <= 0):
                self._log("Take profit is ON but the ATR is not available yet - no TP orders for this trade.")
            else:
                levels = []
                # "Measured from" (2026-10-27): the real entry price (default) or the signal candle close.
                tp_base = hub.tp_base_price(plan, entry_price, snap.close)
                if hub.tp_from(plan) == hub.TP_FROM_SIGNAL:
                    if tp_base == entry_price and snap.close != entry_price:
                        self._log("Take profit is set to 'Signal candle close' but that close is not available - "
                                  "the entry price is used for this trade.")
                    else:
                        self._log(f"Take profit counted from the signal candle close {tp_base:.6g} "
                                  f"(entry price {entry_price:.6g}).")
                for lv in hub.tp_prices(direction, tp_base, atr, plan):
                    levels.append({"index": lv["index"], "on": bool(lv["on"]), "price": float(lv["price"]),
                                   "pct": float(lv["pct"]), "state": "pending" if lv["on"] else "off",
                                   "order_id": None, "qty": None, "recorded_qty": 0.0, "attempts": 0})
                self.hub.tp = {"direction": direction, "entry_price": float(entry_price), "qty0": float(qty),
                               "levels": levels}
                self._log("Take-profit memory: " + ", ".join(
                    f"TP{l['index']} {l['price']:.6g} ({l['pct']:g}%)" for l in levels if l["on"]))
        hub_state.save(self.hub)
        await self._hub_manage_tp()

    # ------------------------------------------------------------------ TAKE PROFIT (one level at a time, remembered)
    async def _hub_manage_tp(self, _from_amend: bool = False) -> None:
        """Every tick while in a position: record what filled, and only when the placed level is DONE place
        the next one (TP1, then TP2, then TP3). The memory is saved to disk after every change."""
        tp = self.hub.tp
        if not tp or self.state.status != "IN_POSITION" or (getattr(self, "_hub_amending", False) and not _from_amend):
            return
        await self._hub_sync_tp_fills()
        levels = tp["levels"]
        if any(l["state"] == "placed" for l in levels):
            return
        nxt = next((l for l in levels if l["state"] == "pending"), None)
        if nxt is not None:
            await self._hub_place_tp(nxt)

    async def _hub_place_tp(self, level: dict) -> None:
        tp = self.hub.tp
        direction = tp["direction"]
        pos = await self.client.get_position_risk(self.symbol)
        if pos is None:
            return
        real_qty = abs(float(pos["positionAmt"]))
        on_levels = [l for l in tp["levels"] if l["on"]]
        last_on = on_levels[-1] if on_levels else None
        if level is last_on and sum(l["pct"] for l in on_levels) >= 99.999:
            qty = real_qty          # the levels add up to 100%: the last one takes everything that is left
        else:
            qty = min(await self.client.round_qty(self.symbol, tp["qty0"] * level["pct"] / 100.0), real_qty)
        qty = await self.client.round_qty(self.symbol, qty)
        if qty <= 0:
            level["state"] = "skipped"
            self._log(f"TP{level['index']}: the size rounds to 0 - this level is skipped.")
            hub_state.save(self.hub)
            return
        price = await self.client.round_price(self.symbol, level["price"])
        side = "SELL" if direction == "LONG" else "BUY"
        try:
            order = await self.client.limit_reduce_order(self.symbol, side, qty, price)
        except Exception as e:
            level["attempts"] += 1
            self._log(f"TP{level['index']}: could not place the limit order ({e}) - attempt {level['attempts']}/3.")
            if level["attempts"] >= 3:
                level["state"] = "skipped"
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    f"TP{level['index']} could not be placed after 3 tries ({e}) - skipped. The max-loss "
                    f"stop and the flip rules still protect the trade.", self.pc.telegram_enabled))
            hub_state.save(self.hub)
            return
        level.update(state="placed", order_id=order.get("orderId"), qty=qty, price=price)
        hub_state.save(self.hub)
        self._log(f"TP{level['index']} placed: {side} {qty} @ {price} (reduce-only limit). "
                  f"The next level is placed only after this one is hit.")

    async def _hub_sync_tp_fills(self) -> float:
        """Asks the exchange about every placed level, records new fills in the ledger, updates the memory.
        Returns the quantity newly recorded by this call."""
        tp = self.hub.tp
        if not tp:
            return 0.0
        newly = 0.0
        changed = False
        for l in tp["levels"]:
            if l["state"] != "placed":
                continue
            try:
                o = await self.client.get_order(self.symbol, l["order_id"])
            except Exception as e:
                self._log(f"TP{l['index']}: could not read the order ({e}) - will check again.")
                continue
            if o is None:
                executed, status = 0.0, "CANCELED"
            else:
                executed = float(o.get("executedQty") or 0)
                status = o.get("status")
            delta = executed - l["recorded_qty"]
            if delta > 1e-12:
                price = (float(o.get("avgPrice") or 0) if o else 0.0) or l["price"]
                await self._hub_record_tp_fill(l, delta, price)
                l["recorded_qty"] = executed
                newly += delta
                changed = True
            if status == "FILLED":
                l["state"] = "filled"
                changed = True
                self._log(f"TP{l['index']} HIT - filled {executed} @ {l['price']}. Placing the next level now.")
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                changed = True
                if executed > 1e-12:
                    l["state"] = "filled"        # cancelled after a partial fill: that part is done
                else:
                    l["attempts"] += 1
                    l["state"] = "pending" if l["attempts"] < 3 else "skipped"
                    self._log(f"TP{l['index']}: the order is gone without a fill ({status}) - "
                              f"{'placing it again' if l['state'] == 'pending' else 'skipped after 3 tries'}.")
        if changed:
            hub_state.save(self.hub)
        return newly

    async def _hub_record_tp_fill(self, level: dict, qty: float, price: float) -> None:
        direction = self.hub.tp["direction"]
        entry = self.state.entry_price or self.hub.tp["entry_price"]
        sign = 1 if direction == "LONG" else -1
        pnl = sign * (price - entry) * qty
        reason = f"TP{level['index']}"
        self._log(f"{reason} fill recorded: {qty} @ {price} (gross {pnl:+.4f}).")
        try:
            await self._record_ledger(direction, qty, price, pnl, reason,
                                      exit_order_id=level["order_id"], include_entry_commission=False)
        except Exception as e:
            self._log(f"Ledger record of {reason} failed ({e}) - the fill itself is unaffected.")
        await self._notify(tg.notify_exit(self.account_name, self.symbol, direction, reason, price, pnl,
                                          self.pc.telegram_enabled))

    async def _hub_cancel_tp_orders(self) -> None:
        """Record what already filled, then cancel every TP limit order of this bot (memory + a sweep by tag)."""
        tp = self.hub.tp
        if tp:
            await self._hub_sync_tp_fills()
            for l in tp["levels"]:
                if l["state"] == "placed" and l["order_id"]:
                    try:
                        await self.client.cancel_regular_order(self.symbol, l["order_id"])
                    except Exception as e:
                        self._log(f"TP{l['index']}: cancel did not go through ({e}) - the sweep below retries.")
                    l["state"] = "cancelled"
            hub_state.save(self.hub)
        try:
            resting = await self.client.get_open_orders(self.symbol)
        except Exception as e:
            self._log(f"Could not list resting orders to sweep TP orders ({e}).")
            return
        prefixes = self.client.client_order_id_prefix
        for o in resting:
            cid = str(o.get("clientOrderId") or o.get("clOrdId") or "")
            if cid.startswith(prefixes):
                try:
                    await self.client.cancel_regular_order(self.symbol, o.get("orderId") or o.get("ordId"))
                except Exception as e:
                    self._log(f"Could not cancel a resting bot order ({e}).")

    # ------------------------------------------------------------------ PART CLOSED OUTSIDE THE BOT (2026-10-24)
    async def _hub_outside_partial(self, real_qty: float) -> None:
        """Part of the open trade was closed OUTSIDE the bot (owner: closes part by hand on Binance / OKX). That
        part goes into the ledger and into the fixed-base trade total, like a take-profit fill. Take-profit fills
        are recorded first (they are not outside closes); a stop-limit that has triggered is the stop working,
        not an outside close. The price and fees come from the exchange's own fill records; if they cannot be
        found, the mark price is used and the ledger row is marked as an estimate (confirmed = False)."""
        if self.state.status != "IN_POSITION" or not self.state.direction or not self.state.entry_price:
            return
        ex = (self.hub.ladder or {}).get("ex") or {}
        if ex.get("triggered"):
            return
        if self.hub.tp:
            await self._hub_sync_tp_fills()
        tp_done = sum(float(l.get("recorded_qty") or 0) for l in (self.hub.tp or {}).get("levels", []))
        bot_qty = float(self.state.bot_qty or self.state.qty or 0)
        expected = bot_qty - tp_done - float(self.hub.outside_closed_qty or 0)
        gone = expected - real_qty
        try:
            step = float((await self.client.get_symbol_info(self.symbol)).qty_step or 0)
        except Exception:
            step = 0.0
        if gone <= max(step * 0.5, 1e-9):
            return
        direction = self.state.direction
        close_side = "SELL" if direction == "LONG" else "BUY"
        known_orders = {str(self.state.entry_order_id)} | {str(l.get("order_id")) for l in
                                                           (self.hub.tp or {}).get("levels", []) if l.get("order_id")}
        seen = set(str(x) for x in (self.hub.outside_fill_ids or []))
        since_ms = float(self.state.opened_at or 0) * 1000.0 - 1000.0
        qty_sum, value, fees, ids, fees_known = 0.0, 0.0, 0.0, [], True
        try:
            fills = await self.client.get_user_trades(self.symbol, limit=100)
        except Exception as e:
            fills = []
            self._log(f"Could not read the exchange fills for the part closed outside the bot ({e}) - the mark "
                      f"price is used as an estimate.")
        for f in sorted(fills or [], key=lambda r: float(r.get("time") or 0)):
            if qty_sum >= gone - 1e-12:
                break
            fid = str(f.get("id"))
            if (str(f.get("side", "")).upper() != close_side or str(f.get("orderId")) in known_orders
                    or fid in seen or float(f.get("time") or 0) < since_ms):
                continue
            try:
                q, px = float(f.get("qty") or 0), float(f.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if q <= 0 or px <= 0:
                continue
            q = min(q, gone - qty_sum)
            qty_sum += q
            value += q * px
            if f.get("commission") is None:
                fees_known = False
            else:
                fees += float(f.get("commission") or 0) * (q / float(f.get("qty")))
            ids.append(fid)
        confirmed = qty_sum >= gone - max(step * 0.5, 1e-9)
        if not confirmed:
            try:
                mark = float(await self.client.get_mark_price(self.symbol))
            except Exception:
                mark = None
            if mark:
                value += (gone - qty_sum) * mark
            qty_sum = gone if mark else qty_sum
        if qty_sum <= 0:
            self._log(f"{gone:g} of the trade was closed outside the bot, but its price is unknown - not recorded.")
            return
        price = value / qty_sum
        sign = 1 if direction == "LONG" else -1
        gross = sign * (price - float(self.state.entry_price)) * qty_sum
        fees_in = bool(ids) and fees_known and confirmed   # an estimated part has no known fee: gross
        net = gross - fees if fees_in else gross
        self._hub_note_result(net)   # fixed base amount: this part counts in the trade total
        try:
            ledger.record_trade(ledger.TradeRecord(
                account_id=self.account_id, symbol=self.symbol, direction=direction, qty=qty_sum,
                entry_price=float(self.state.entry_price), exit_price=price, pnl=net, reason="manual_partial_close",
                confirmed=confirmed, opened_at=self.state.opened_at or 0.0, closed_at=time.time(),
                commission=fees if fees_in else None, commission_included=fees_in))
        except Exception as e:
            self._log(f"Ledger record of the part closed outside the bot failed ({e}).")
        self.hub.outside_closed_qty = float(self.hub.outside_closed_qty or 0) + qty_sum
        self.hub.outside_fill_ids = list(self.hub.outside_fill_ids or []) + ids
        hub_state.save(self.hub)
        self._log(f"Part closed outside the bot: {qty_sum:g} @ {price:.8g} ({'from the exchange fills' if confirmed else 'price is an ESTIMATE from the mark price'}), "
                  f"result {net:+.4f}{' net of fees' if fees_in else ' gross'}. Recorded in the ledger and the trade total.")

    async def _hub_qty_after_tp(self, qty: float) -> float:
        """For a close that happened outside the bot's own close: sync the TP fills first and return the
        quantity that was NOT already closed by take-profit orders (<= 0 means TP closed everything)."""
        if not self.hub.tp:
            return qty
        newly = await self._hub_sync_tp_fills()
        remaining = qty - newly
        bot_qty = self.state.bot_qty or qty
        done = sum(l["recorded_qty"] for l in self.hub.tp["levels"])
        if done >= 0.999 * bot_qty:
            return 0.0
        return max(remaining, 0.0)

    # ------------------------------------------------------------------ STOP LADDER (owner design 2026-10-23)
    # Three levels protect a trade: Max Loss (the last line of defense), Stop Loss (the normal stop of the trade)
    # and the Trailing Stop (replaces the Stop Loss once it takes charge, only tightens). The NEAREST level rests
    # on the exchange: Stop Loss / Trailing Stop as a stop-LIMIT (trigger = limit price), Max Loss as a
    # stop-MARKET. The levels behind it stay in the bot's memory (saved with the trade, restored after a restart):
    # if the live price goes THROUGH one of them while the trade is still open, the bot closes at market.
    def _hub_any_stop(self) -> bool:
        p = self._plan
        return bool(p["sizing"]["stop_on"] or p["sl"]["on"] or p["trail"]["on"] or hub.sr_stop_on(p))

    def _hub_new_ladder(self, direction: str, entry_price: float, qty: float, leverage, available_balance: float,
                        snap) -> dict:
        p = self._plan
        ml = self._hub_stop_target(direction, entry_price, qty, leverage, available_balance) if p["sizing"]["stop_on"] else None
        sl = hub.sl_level(direction, entry_price, getattr(snap, "sl_atr", float("nan")), p, snap=snap, qty=qty)
        step = hub.step_setup(direction, entry_price, getattr(snap, "ts_atr", float("nan")), p, sl)
        if p["trail"]["on"] and p["trail"]["type"] == hub.TRAIL_STEP and step is None:
            self._log("Trailing Stop (Step) is ON but its start or gap cannot be worked out (ATR not available yet, "
                      "or 'Same as Stop Loss' without a Stop Loss level) - no Step trailing for this trade.")
        be = hub.be_setup(direction, entry_price, getattr(snap, "be_atr", float("nan")), p)
        if be is not None and p["be"].get("after_atr") and be["trigger"] is None:
            self._log("Break-Even After ATR: the ATR is not available yet - only After TP (if ticked) can move it.")
        # Trailing Stop "Signal candle high / low +/- ATR" (2026-10-28): the first level is set at the entry from the
        # signal candle; the distance (ATR of the signal candle) is kept for the whole trade.
        ts, tc_dist, tc_arm = None, None, None
        if p["trail"]["on"] and p["trail"]["type"] == hub.TRAIL_SUP:
            # Trailing Stop "Supertrend line": the line of the signal candle, if it is on the protective side
            ts = hub.sup_trail_level(direction, getattr(snap, "sup_line", float("nan")), entry_price)
            if ts is None:
                self._log("Trailing Stop (Supertrend line): the line is on the wrong side of the entry price (or not "
                          "worked out yet) - it takes charge at a later candle close.")
            else:
                self._log(f"Trailing Stop (Supertrend line): first level {ts:.8g}.")
        if p["trail"]["on"] and p["trail"]["type"] == hub.TRAIL_CANDLE:
            tc_dist = hub.candle_trail_distance(p, entry_price, getattr(snap, "ts_atr", float("nan")))
            ts = hub.candle_trail_level(direction, snap.low, snap.high, tc_dist)
            if ts is None:
                tc_dist = None
                self._log("Trailing Stop (Signal candle +/- ATR) is ON but its level cannot be worked out (ATR not "
                          "available yet) - no trailing for this trade.")
            elif hub.candle_start_profit(p):
                # "Only once in profit": no level at the entry; it may take charge after a candle closes far enough
                tc_arm = hub.candle_arm_price(direction, entry_price, getattr(snap, "ts_atr", float("nan")), p)
                ts = None
                if tc_arm is None:
                    self._log("Trailing Stop (Signal candle +/- ATR, only once in profit): the profit distance cannot "
                              "be worked out (ATR not available yet) - no trailing for this trade.")
                else:
                    self._log(f"Trailing Stop (Signal candle +/- ATR) waits: it may take charge after a candle closes "
                              f"at {tc_arm:.8g} or better.")
            else:
                self._log(f"Trailing Stop (Signal candle +/- ATR): first level {ts:.8g} "
                          f"(signal candle {'low' if direction == 'LONG' else 'high'} "
                          f"{'-' if direction == 'LONG' else '+'} {tc_dist:.8g}).")
        lad = {"direction": direction, "entry": float(entry_price), "ml": ml, "sl": sl, "ts": ts,
               "best": float(entry_price), "step": step, "ex": None, "be": be}
        if tc_dist is not None:
            lad["tc_dist"] = float(tc_dist)
        if tc_arm is not None:
            lad["tc_arm"] = float(tc_arm)
        return lad

    def _hub_ladder_ensure(self) -> dict | None:
        """The ladder of the open position. A ladder that does not belong to it (other side / other entry) or a
        trade opened before 2026-10-23 is rebuilt from what the bot knows: the resting stop is taken as the Max
        Loss (or the trailing level when Max Loss is off). Nothing is invented."""
        d, entry = self.state.direction, self.state.entry_price
        if not d or not entry:
            return None
        lad = self.hub.ladder
        if lad and lad.get("direction") == d and lad.get("entry") and abs(lad["entry"] - entry) <= 0.005 * entry:
            return lad
        known = self.state.stop_target if self.state.stop_target is not None else (
            self.state.sl_price if self.state.sl_order_id else None)
        name = "ml" if self._plan["sizing"]["stop_on"] else "ts"
        lad = {"direction": d, "entry": float(entry), "ml": None, "sl": None, "ts": None, "best": float(entry),
               "step": None, "ex": None}
        if known is not None:
            lad[name] = float(known)
        # Levels that need no ATR (measured in % of price movement) can be worked out again from the entry price.
        lad["sl"] = hub.sl_level(d, float(entry), float("nan"), self._plan, qty=self.state.bot_qty or self.state.qty)
        lad["step"] = hub.step_setup(d, float(entry), float("nan"), self._plan, lad["sl"])
        lad["be"] = hub.be_setup(d, float(entry), float("nan"), self._plan)   # After TP still works (no ATR here)
        if self.state.sl_order_id and self.state.sl_price is not None:
            lad["ex"] = {"name": name, "kind": "market", "price": float(self.state.sl_price), "qty": None,
                         "algo_id": self.state.sl_order_id, "triggered": False}
        self.hub.ladder = lad
        hub_state.save(self.hub)
        self._log("Stop ladder rebuilt for the open position from the stop the bot knows "
                  f"({hub.LADDER_NAMES[name]} {known}).")
        return lad

    async def _hub_ladder_fill_max_loss(self) -> None:
        """Recovery of a position whose Max Loss level is not known (no saved state): it is worked out from the
        plan, from the balance at entry if saved, else from the current available balance (logged loudly) -
        protecting a real position beats leaving it naked. Same rule as the Base V3 recovery."""
        lad = self.hub.ladder
        if not lad or lad.get("ml") is not None or not self._plan["sizing"]["stop_on"]:
            return
        basis = self.state.equity_at_entry
        if not basis:
            basis = await self.client.get_available_balance()
            self._log(f"No saved Max Loss and no saved balance-at-entry for this position - working out the Max "
                      f"Loss from the CURRENT available balance ({basis:.2f}) as a best effort.")
            self.state.equity_at_entry = basis
        lad["ml"] = float(self._hub_stop_target(lad["direction"], lad["entry"],
                                                self.state.bot_qty or self.state.qty,
                                                self.state.leverage or self._plan["sizing"]["leverage"], basis))
        self.state.stop_target = lad["ml"]
        hub_state.save(self.hub)
        self._persist_trade_state()

    def _hub_ladder_text(self) -> str:
        lad = self.hub.ladder or {}
        parts = [f"{hub.LADDER_NAMES[k]} {lad.get(k):.8g}" for k in ("ts", "sl", "ml") if lad.get(k) is not None]
        return "stops: " + (", ".join(parts) if parts else "none")

    def _hub_live_price(self) -> float | None:
        """The live mark price from the price feed (about every second), or None when the feed is stale."""
        feed = getattr(self, "mark_feed", None)
        if feed is None:
            return None
        try:
            if feed.is_stale():
                return None
        except Exception:
            return None
        p = getattr(feed, "last_mark_price", None)
        try:
            p = float(p) if p is not None else None
        except (TypeError, ValueError):
            return None
        return p if p and math.isfinite(p) and p > 0 else None

    async def _hub_ladder_check(self, mark: float | None = None) -> bool:
        """Runs on every tick (REST price) and about every second between ticks (live feed price).
        1. Step trailing follows the best price.  2. A memory level the price went THROUGH while the trade is
        still open -> close at market.  3. The nearest level is the one resting on the exchange.
        Returns True if it closed (or started closing) the trade."""
        if not self._is_hub() or self.state.status != "IN_POSITION" or not self.state.direction:
            return False
        if getattr(self, "_hub_amending", False):
            return False          # the owner's change of the open trade is being applied (a second or two)
        lad = self._hub_ladder_ensure()
        if lad is None:
            return False
        if mark is None:
            mark = await self.client.get_mark_price(self.symbol)
        d = lad["direction"]
        changed = False
        step = lad.get("step")
        if step:
            if (d == "LONG" and mark > lad["best"]) or (d == "SHORT" and mark < lad["best"]):
                lad["best"] = float(mark)
                changed = True
            lvl = hub.step_level(d, lad["entry"], step["start"], step["gap"], lad["best"],
                                 step.get("begin"), step.get("first"))
            if lvl is not None and hub.is_tighter(d, lvl, lad.get("ts")):
                first = lad.get("ts") is None
                lad["ts"] = float(lvl)
                changed = True
                self._log(f"Trailing Stop (Step) {'takes charge' if first else 'moves'} at {lvl:.8g} "
                          f"(best price {lad['best']:.8g}).")
        # BREAK-EVEN: after TP n / the After ATR move -> the Stop Loss goes to the entry price (only tightens)
        be = lad.get("be")
        filled = {l["index"] for l in ((self.hub.tp or {}).get("levels") or []) if l.get("state") == "filled"}
        if hub.be_due(d, be, mark, filled):
            be["done"] = True
            changed = True
            if hub.is_tighter(d, lad["entry"], lad.get("sl")):
                lad["sl"] = float(lad["entry"])
                self._log(f"Break-Even: the Stop Loss moves to the entry price {lad['entry']:.8g} "
                          f"(mark {mark:.8g}).")
        if changed:
            hub_state.save(self.hub)

        ex = lad.get("ex") or {}
        ex_name = ex.get("name")
        # AUDIT FIX A: the stop-limit on the exchange was gone through and the trade is still open -> close at market.
        if ex.get("kind") == "limit" and ex.get("price") is not None:
            if hub.passed(d, float(ex["price"]), mark):
                t0 = lad.get("ex_passed_at")
                if t0 is None:
                    lad["ex_passed_at"] = time.time()
                    hub_state.save(self.hub)
                elif time.time() - float(t0) >= STOP_LIMIT_GRACE_S:
                    pos = await self.client.get_position_risk(self.symbol)
                    if pos is not None:
                        lvl_name = hub.LADDER_NAMES.get(ex_name, "stop")
                        msg = (f"The price {mark:.8g} went through the {lvl_name} stop-limit at {float(ex['price']):.8g} "
                               f"and the trade is still open (the limit did not fill) - closing at market now.")
                        self._log(msg)
                        await self._notify(tg.notify_error(self.account_name, self.symbol, msg,
                                                           self.pc.telegram_enabled))
                        await self._hub_cancel_stop_child()
                        await self._close_position(reason=f"{lvl_name.lower().replace(' ', '_')}_limit_not_filled")
                        return True
            elif lad.pop("ex_passed_at", None) is not None:
                hub_state.save(self.hub)
        for name in hub.LADDER_ORDER:
            lv = lad.get(name)
            if lv is None or name == ex_name:
                continue
            if hub.passed(d, lv, mark):
                msg = (f"The price {mark:.8g} went through the {hub.LADDER_NAMES[name]} level {lv:.8g} and the trade "
                       f"is still open (the stop on the exchange did not close it) - closing at market now.")
                self._log(msg)
                await self._notify(tg.notify_error(self.account_name, self.symbol, msg, self.pc.telegram_enabled))
                await self._close_position(reason=f"{hub.LADDER_NAMES[name].lower().replace(' ', '_')}_backup")
                return True
        return await self._hub_sync_exchange_stop(mark)

    async def _hub_sync_exchange_stop(self, mark: float | None, first: bool = False) -> bool:
        """Puts the NEAREST ladder level on the exchange (new order first, then the old one is cancelled - there is
        never a moment without a stop). A stop-limit is put back when the position size changed (take-profit fill).
        first=True (entry / recovery): a failure raises, so the caller retries or protects/flattens.
        Returns True if the level was already crossed and the trade was closed at market."""
        lad = self._hub_ladder_ensure()
        if lad is None:
            return False
        d = lad["direction"]
        name, price = hub.nearest_level(d, lad)
        if name is None:
            return False
        ex = lad.get("ex")
        if ex and ex.get("triggered") and self.state.sl_order_id == ex.get("algo_id"):
            return False   # a stop-limit already triggered: its limit order is working; the memory levels watch
        kind = "market" if name == "ml" else "limit"
        price_r = await self.client.round_price(self.symbol, price)
        qty = None
        if kind == "limit":
            qty = await self.client.round_qty(self.symbol, float(self.state.qty or 0))
            if qty <= 0:
                return False
        if (ex and self.state.sl_order_id and ex.get("algo_id") == self.state.sl_order_id
                and ex.get("name") == name and ex.get("kind") == kind
                and abs(float(ex.get("price") or 0) - price_r) <= 1e-12
                and (kind == "market" or abs(float(ex.get("qty") or 0) - qty) <= 1e-12)):
            return False   # already resting as it should
        if kind == "limit" and not first and time.time() < float(lad.get("limit_retry_at") or 0):
            return False   # a refused stop-limit is tried again a little later, not every second
        if mark is None:
            mark = await self.client.get_mark_price(self.symbol)
        ok, _ = self.client.validate_stop_side(d, price_r, mark)
        if not ok:
            reason = "stop_crossed_at_entry" if first else hub.LADDER_NAMES[name].lower().replace(" ", "_")
            msg = (f"The {hub.LADDER_NAMES[name]} level {price_r} is already crossed by the price (mark {mark}) - "
                   f"closing at market.")
            self._log(msg)
            await self._notify(tg.notify_error(self.account_name, self.symbol, msg, self.pc.telegram_enabled))
            await self._close_position(reason=reason)
            return True

        close_side = "SELL" if d == "LONG" else "BUY"
        tpx = getattr(self.pc, "trigger_px_type", "mark")
        old_id = self.state.sl_order_id
        try:
            if kind == "market":
                order = await self.client.stop_market_order(self.symbol, close_side, price_r,
                                                            skip_throttle=True, trigger_px_type=tpx)
            else:
                order = await self.client.stop_limit_order(self.symbol, close_side, price_r, price_r, qty,
                                                           skip_throttle=(first or old_id is None),
                                                           trigger_px_type=tpx)
        except Exception as e:
            if kind == "limit":
                lad["limit_retry_at"] = time.time() + 60
                hub_state.save(self.hub)
                note = (f"The exchange refused the {hub.LADDER_NAMES[name]} stop-limit at {price_r} ({e}).")
                if not lad.get("limit_refused_notified"):
                    lad["limit_refused_notified"] = True
                    hub_state.save(self.hub)
                    await self._notify(tg.notify_error(self.account_name, self.symbol,
                                                       note + " The bot keeps the levels in memory and tries again.",
                                                       self.pc.telegram_enabled))
                self._log(note)
                # No stop on the exchange at all: the next level down the ladder that is a stop-market (Max Loss)
                # goes on instead, so the position is never left without a resting stop.
                if old_id is None and lad.get("ml") is not None and name != "ml":
                    ml_r = await self.client.round_price(self.symbol, lad["ml"])
                    if self.client.validate_stop_side(d, ml_r, mark)[0]:
                        order = await self.client.stop_market_order(self.symbol, close_side, ml_r,
                                                                    skip_throttle=True, trigger_px_type=tpx)
                        self._hub_record_ex(lad, "ml", "market", ml_r, None, order.get("algoId"), None)
                        self._log(f"Max Loss stop-market placed at {ml_r} instead (the last line of defense).")
                        return False
                if first and old_id is None:
                    raise
                return False
            if first and old_id is None:
                raise
            self._log(f"Could not place the {hub.LADDER_NAMES[name]} stop at {price_r} ({e}) - the old stop stays.")
            await self._notify(tg.notify_error(self.account_name, self.symbol,
                                               f"{hub.LADDER_NAMES[name]} could not move to {price_r} ({e}). "
                                               f"The old stop stays.", self.pc.telegram_enabled))
            return False
        self._hub_record_ex(lad, name, kind, price_r, qty, order.get("algoId"), old_id)
        if old_id and old_id != order.get("algoId"):
            try:
                await self.client.cancel_algo_order(old_id)
            except Exception as e:
                self._log(f"The old stop {old_id} was not cancelled ({e}) - it is cleaned up at the next check.")
        self._log(f"Stop on the exchange: {hub.LADDER_NAMES[name]} at {price_r} "
                  f"({'stop-limit, qty ' + str(qty) if kind == 'limit' else 'stop-market'}). {self._hub_ladder_text()}.")
        if not first:
            await self._notify(tg.notify_sl_update(self.account_name, self.symbol, d, price_r, False,
                                                   self.pc.telegram_enabled))
        return False

    async def _hub_reconcile_stops(self) -> tuple:
        """Restart with an open position: keep exactly ONE of the bot's own stops (a stop-market or a stop-limit),
        the one the ladder says rests on the exchange if it is still there, else the newest; cancel the rest.
        Returns (algo_id, trigger_price) or (None, None)."""
        try:
            open_orders = await self.client.get_open_algo_orders(self.symbol)
        except Exception as e:
            self._log(f"Could not list the resting stops ({e}).")
            raise
        own = [o for o in open_orders
               if str(o.get("clientAlgoId", "")).startswith(self.client.client_order_id_prefix)
               and o.get("orderType") in ("STOP_MARKET", "STOP")]
        if not own:
            return None, None
        lad = self.hub.ladder or {}
        ex = lad.get("ex") or {}
        own.sort(key=lambda o: o["algoId"])
        keep = next((o for o in own if ex.get("algo_id") is not None and o["algoId"] == ex.get("algo_id")), own[-1])
        for o in own:
            if o["algoId"] != keep["algoId"]:
                self._log(f"Cancelling an extra stop of the bot (algoId {o['algoId']}) - one stop rests per trade.")
                try:
                    await self.client.cancel_algo_order(o["algoId"])
                except ExchangeAPIError:
                    pass
        if lad and ex.get("algo_id") != keep["algoId"]:
            kind = "limit" if keep.get("orderType") == "STOP" else "market"
            lad["ex"] = {"name": ex.get("name") or ("ml" if kind == "market" else "sl"), "kind": kind,
                         "price": float(keep["triggerPrice"]), "qty": None, "algo_id": keep["algoId"],
                         "triggered": False}
            hub_state.save(self.hub)
        return keep["algoId"], float(keep["triggerPrice"])

    def _hub_record_ex(self, lad: dict, name: str, kind: str, price: float, qty, algo_id, old_id) -> None:
        lad["ex"] = {"name": name, "kind": kind, "price": float(price), "qty": qty, "algo_id": algo_id,
                     "triggered": False}
        lad.pop("limit_retry_at", None)
        lad.pop("ex_passed_at", None)
        if kind == "limit":
            lad.pop("limit_refused_notified", None)
        self.state.sl_order_id = algo_id
        self.state.sl_price = float(price)
        hub_state.save(self.hub)
        self._persist_trade_state()

    # ------------------------------------------------------------------ CHANGE AN OPEN TRADE (owner 2026-11-05)
    async def _hub_entry_snapshot(self, plan: dict):
        """The snapshot of the SIGNAL candle of the open trade, worked out with PLAN (so a changed ATR length is
        used). Pure read: unlike _hub_fetch it changes nothing on the bot. None when the candle cannot be found."""
        t0 = self.state.entry_candle_time
        if t0 is None:
            return None
        raw = await self.client.get_klines(self.symbol, self.pc.timeframe, limit=hub.HUB_HISTORY_CANDLES + 1)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbbav", "tbqav", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype("int64")
        closed = df.iloc[:-1].reset_index(drop=True)
        if len(closed) < HUB_MIN_BARS:
            return None
        frame = hub.compute_hub(closed, plan)
        idx = frame.index[frame["open_time"] == int(t0)]
        if not len(idx):
            return None
        return hub.hub_snapshot_at(frame, int(idx[0]))

    async def hub_amend_open_trade(self, new_plan: dict, stops: str) -> dict:
        """See _hub_amend_open_trade_inner. While it runs, the bot's own take-profit placing and memory-level watch
        wait (a second or two), so they never work on half-changed levels."""
        if getattr(self, "_hub_amending", False):
            raise RuntimeError("A change of the open trade is already being applied.")
        self._hub_amending = True
        try:
            return await self._hub_amend_open_trade_inner(new_plan, stops)
        finally:
            self._hub_amending = False

    async def _hub_amend_open_trade_inner(self, new_plan: dict, stops: str) -> dict:
        """Apply the allowed settings of NEW_PLAN (hub.AMEND_PATHS) to the trade that is open now.
        stops: "tighter" = a stop level changes only if the new one protects MORE (a stop is never removed or moved
        away); "looser" = the new levels are used as they are, also when they protect less. The new levels are
        worked out as if the new setting had been on at the entry (signal candle, entry price, entry amount).
        Everything else in NEW_PLAN waits for the next trade. Returns {"applied": [...], "kept": [...], "waits": bool}.
        Raises RuntimeError when there is no open trade."""
        if stops not in (hub.AMEND_TIGHTER, hub.AMEND_LOOSER):
            raise ValueError("stops must be 'tighter' or 'looser'")
        if (not self._is_hub() or self.state.status != "IN_POSITION" or not self.state.direction
                or not self.state.entry_price):
            raise RuntimeError("There is no open trade to change.")
        res = {"applied": [], "kept": [], "waits": False}
        applied, kept = res["applied"], res["kept"]
        diff = hub.amend_diff(self._plan, new_plan)
        res["waits"] = bool(diff["waits"])
        groups = diff["now"]
        if not groups:
            return res
        merged = hub.amend_merge(self._plan, new_plan)
        lad = self._hub_ladder_ensure()
        if lad is None:
            raise RuntimeError("There is no open trade to change.")
        d, entry = lad["direction"], float(lad["entry"])
        qty = float(self.state.bot_qty or self.state.qty or 0)
        nan = float("nan")
        looser = stops == hub.AMEND_LOOSER

        snap = None
        if any(g in groups for g in ("Trailing Stop", "Stop Loss", "Break-Even", "Take profit")):
            try:
                snap = await self._hub_entry_snapshot(merged)
            except Exception as e:
                self._log(f"Change open trade: the signal candle could not be read ({e}).")
            if snap is None:
                self._log("Change open trade: the signal candle is not available - settings that need it (ATR, "
                          "candle high / low) cannot be worked out and stay as they are.")

        def val(name):
            return getattr(snap, name, nan) if snap is not None else nan

        def adopt(key: str, label: str, new_lvl, wanted: bool) -> None:
            old_lvl = lad.get(key)
            if not wanted:
                if old_lvl is None:
                    return
                if looser:
                    lad[key] = None
                    applied.append(f"{label}: turned off - its level {old_lvl:.8g} is removed.")
                else:
                    kept.append(f"{label}: turned off in the new rules, but the open trade keeps its level "
                                f"{old_lvl:.8g} (\"Only tighter\" never takes a stop away).")
                return
            if new_lvl is None or not math.isfinite(new_lvl):
                kept.append(f"{label}: the new level cannot be worked out now - the open trade keeps "
                            f"{('%.8g' % old_lvl) if old_lvl is not None else 'none'}.")
                return
            new_lvl = float(new_lvl)
            if old_lvl is not None and abs(new_lvl - old_lvl) <= 1e-12 * max(1.0, abs(new_lvl)):
                return
            if old_lvl is None or looser or hub.is_tighter(d, new_lvl, old_lvl):
                lad[key] = new_lvl
                applied.append(f"{label}: now {new_lvl:.8g}" + (f" (was {old_lvl:.8g})." if old_lvl is not None else "."))
            else:
                kept.append(f"{label}: the new level {new_lvl:.8g} protects less than the current {old_lvl:.8g} - "
                            f"kept the current one (choose \"Allow looser too\" to use the new one).")

        # --- Max Loss per Trade
        if "Max Loss per Trade" in groups:
            ml_new = None
            if merged["sizing"]["stop_on"]:
                try:
                    bal = self.state.equity_at_entry
                    if not bal:
                        bal = await self.client.get_available_balance()
                        self._log(f"Change open trade: no saved balance-at-entry - Max Loss worked out from the "
                                  f"CURRENT available balance ({bal:.2f}).")
                    lev = self.state.leverage or merged["sizing"]["leverage"]
                    margin = qty * entry / float(lev or 1)
                    loss = hub.max_loss_usd(merged, float(bal), margin)
                    cand = hub.stop_price_from_usd(d, entry, qty, loss)
                    ml_new = cand if cand > 0 else None
                except Exception as e:
                    self._log(f"Change open trade: Max Loss could not be worked out ({e}).")
            adopt("ml", "Max Loss per Trade", ml_new, bool(merged["sizing"]["stop_on"]))

        # --- Stop Loss
        if "Stop Loss" in groups:
            adopt("sl", "Stop Loss", hub.sl_level(d, entry, val("sl_atr"), merged, snap=snap, qty=qty),
                  bool(merged["sl"]["on"]))

        # --- Trailing Stop
        if "Trailing Stop" in groups:
            tr = merged["trail"]
            if not tr["on"]:
                adopt("ts", "Trailing Stop", None, False)
                if looser:
                    lad["step"] = None
                    lad.pop("tc_dist", None)
            else:
                ttype = tr["type"]
                new_step = hub.step_setup(d, entry, val("ts_atr"), merged, lad.get("sl"))
                if ttype == hub.TRAIL_STEP and new_step is None:
                    pass                      # cannot be worked out now: the step the trade has stays
                else:
                    lad["step"] = new_step
                tc_dist = hub.candle_trail_distance(merged, entry, val("ts_atr"))
                if tc_dist is not None:
                    lad["tc_dist"] = float(tc_dist)
                elif ttype != hub.TRAIL_CANDLE:
                    lad.pop("tc_dist", None)
                arm = hub.candle_arm_price(d, entry, val("ts_atr"), merged)
                profit_wait = hub.candle_start_profit(merged)
                if arm is not None:
                    lad["tc_arm"] = float(arm)
                else:
                    lad.pop("tc_arm", None)
                ts_init = None
                if ttype == hub.TRAIL_CANDLE and tc_dist is not None and snap is not None:
                    ts_init = hub.candle_trail_level(d, snap.low, snap.high, tc_dist)
                elif ttype == hub.TRAIL_SUP and snap is not None:
                    ts_init = hub.sup_trail_level(d, val("sup_line"), entry)
                elif ttype == hub.TRAIL_STEP and lad["step"]:
                    st = lad["step"]
                    ts_init = hub.step_level(d, entry, st["start"], st["gap"], lad.get("best", entry),
                                             st.get("begin"), st.get("first"))
                if profit_wait and arm is None:
                    kept.append("Trailing Stop (only once in profit): the profit distance cannot be worked out now - "
                                "the current level stays.")
                elif profit_wait:
                    if looser and lad.get("ts") is not None:
                        lad["ts"] = None
                    applied.append(f"Trailing Stop: it may take charge once a candle closes at {arm:.8g} or better.")
                elif ttype in (hub.TRAIL_LLHH, hub.TRAIL_MA):
                    # its first level comes at the next candle close
                    if looser and lad.get("ts") is not None:
                        lad["ts"] = None
                        applied.append(f"Trailing Stop: the old level is removed - the new {ttype} trailing takes "
                                       f"charge at the next candle close.")
                    else:
                        applied.append(f"Trailing Stop: the new {ttype} rule starts at the next candle close.")
                elif ts_init is None:
                    kept.append("Trailing Stop: its first level cannot be worked out now - the new rule starts at the "
                                "next candle close; the current level stays until then.")
                    if ttype == hub.TRAIL_STEP and new_step is None:
                        kept.append("Trailing Stop (Step): its start or gap cannot be worked out - the Step the trade "
                                    "has stays.")
                else:
                    adopt("ts", "Trailing Stop", ts_init, True)

        # --- Break-Even
        if "Break-Even" in groups:
            old_be = lad.get("be")
            if not merged["be"]["on"]:
                if looser:
                    lad["be"] = None
                    applied.append("Break-Even: turned off.")
                elif old_be is not None:
                    kept.append("Break-Even: turned off in the new rules, but the open trade keeps it.")
            else:
                be_new = hub.be_setup(d, entry, val("be_atr"), merged)
                if be_new is None:
                    kept.append("Break-Even: cannot be worked out now - kept as it was.")
                else:
                    be_new["done"] = bool(old_be and old_be.get("done"))
                    lad["be"] = be_new
                    applied.append("Break-Even: updated" + (" (it had already moved the stop)." if be_new["done"] else "."))

        # --- Take profit
        if "Take profit" in groups:
            await self._hub_amend_tp(merged, d, entry, qty, snap, res)

        # --- Exit when / Hold rule: read at every candle close from the rules of the open trade
        if "Exit when" in groups:
            applied.append("Exit when: updated - it is read at the next candle close.")
        if "Hold rule ADX level" in groups:
            applied.append("Hold rule ADX level: updated - it is read at the next candle close.")

        if self.state.status != "IN_POSITION" or self.hub.ladder is not lad:
            raise RuntimeError("The trade closed (or changed) while the change was being worked out - nothing was "
                               "changed on it. The new rules start from the next trade.")
        self._plan = merged
        self.state.stop_target = lad.get("ml") if lad.get("ml") is not None else self.state.stop_target
        hub_state.save(self.hub)
        self._persist_trade_state()
        self._log("Change open trade (" + ("allow looser" if looser else "only tighter") + "): "
                  + (" ".join(applied) if applied else "nothing changed.")
                  + ((" Kept: " + " ".join(kept)) if kept else "") + " " + self._hub_ladder_text())
        try:
            await self._hub_sync_exchange_stop(None)
        except Exception as e:
            self._log(f"Change open trade: the stop on the exchange could not be updated now ({e}) - it is "
                      f"tried again at the next check.")
            kept.append("The stop on the exchange is updated at the next check.")
        return res

    async def _hub_amend_tp(self, merged: dict, d: str, entry: float, qty: float, snap, res: dict) -> None:
        """Take profit of the open trade: the levels already FILLED stay; a level that is resting on the exchange is
        cancelled and the new levels (pending) are placed one at a time as always. Nothing is cancelled unless the
        new levels can be worked out first."""
        applied, kept = res["applied"], res["kept"]
        new_tp = merged["tp"]
        if not new_tp["on"] and not (self.hub.tp or {}).get("levels"):
            return                                # was off, stays off: nothing to change
        try:
            await self._hub_sync_tp_fills()
        except Exception as e:
            kept.append(f"Take profit: could not check the fills ({e}) - kept as it was.")
            return
        mem = self.hub.tp
        old_levels = (mem or {}).get("levels") or []
        filled = {l["index"] for l in old_levels if l.get("state") == "filled"}
        new_levels = []
        if new_tp["on"]:
            atr = getattr(snap, "tp_atr", float("nan")) if snap is not None else float("nan")
            if new_tp.get("basis", hub.BASIS_ATR) == hub.BASIS_ATR and (not math.isfinite(atr) or atr <= 0):
                kept.append("Take profit: the ATR of the signal candle is not available - kept as it was.")
                return
            close = getattr(snap, "close", entry) if snap is not None else entry
            base = hub.tp_base_price(merged, entry, close)
            for lv in hub.tp_prices(d, base, atr, merged):
                if lv["index"] in filled:
                    continue
                new_levels.append({"index": lv["index"], "on": bool(lv["on"]), "price": float(lv["price"]),
                                   "pct": float(lv["pct"]), "state": "pending" if lv["on"] else "off",
                                   "order_id": None, "qty": None, "recorded_qty": 0.0, "attempts": 0})
        # cancel the level that rests on the exchange
        for l in old_levels:
            if l.get("state") == "placed" and l.get("order_id"):
                try:
                    await self.client.cancel_regular_order(self.symbol, l["order_id"])
                except Exception as e:
                    await self._hub_sync_tp_fills()       # maybe it filled in the meantime
                    if l.get("state") == "placed":
                        kept.append(f"Take profit: TP{l['index']} could not be cancelled ({e}) - Take profit kept as "
                                    f"it was.")
                        return
        keep_filled = [l for l in old_levels if l.get("state") == "filled"]
        if mem is None:
            mem = {"direction": d, "entry_price": float(entry), "qty0": float(qty), "levels": []}
        mem["levels"] = sorted(keep_filled + new_levels, key=lambda x: x["index"])
        self.hub.tp = mem if (new_levels or keep_filled) else None
        hub_state.save(self.hub)
        if new_tp["on"]:
            applied.append("Take profit: updated - " + (", ".join(
                f"TP{l['index']} {l['price']:.6g} ({l['pct']:g}%)" for l in new_levels if l["on"]) or "no new level")
                + (f"; TP{'/TP'.join(str(i) for i in sorted(filled))} already filled stay." if filled else "."))
            await self._hub_manage_tp(_from_amend=True)
        else:
            applied.append("Take profit: turned off - the waiting levels are cancelled"
                           + (" (filled ones stay)." if filled else "."))

    async def _hub_trail(self, snap) -> bool:
        """Candle close: the Lowest low / highest high and the Moving average Trailing Stop get their new level here
        (the Step type moves live in _hub_ladder_check). The level takes charge only when it is tighter than the
        stop in charge, and it only tightens. Returns True if it closed the position."""
        plan = self._plan
        if not plan["trail"]["on"] or self.state.status != "IN_POSITION" or not self.state.direction:
            return False
        lad = self._hub_ladder_ensure()
        if lad is None:
            return False
        d = lad["direction"]
        t = plan["trail"]["type"]
        mark = None
        if t in (hub.TRAIL_LLHH, hub.TRAIL_MA, hub.TRAIL_CANDLE, hub.TRAIL_SUP):
            if t == hub.TRAIL_LLHH:
                cand = snap.trail_low if d == "LONG" else snap.trail_high
            elif t == hub.TRAIL_SUP:
                cand = getattr(snap, "sup_line", float("nan"))      # the Supertrend line of the candle that just closed
            elif t == hub.TRAIL_CANDLE:
                # the candle that just closed, with the distance fixed at the signal candle
                cand = hub.candle_trail_level(d, snap.low, snap.high, lad.get("tc_dist"))
                if (hub.candle_start_profit(plan) and lad.get("ts") is None
                        and not hub.candle_armed(d, snap.close, lad.get("tc_arm"))):
                    cand = None             # "Only once in profit": no candle has closed far enough yet
            else:
                cand = snap.ma1 if plan["trail"]["ma"] == "MA 1" else snap.ma2
            if cand is not None and math.isfinite(cand):
                cand = await self.client.round_price(self.symbol, cand)
                _, in_charge = hub.nearest_level(d, lad)
                if hub.is_tighter(d, cand, lad.get("ts")) and hub.is_tighter(d, cand, in_charge):
                    mark = await self.client.get_mark_price(self.symbol)
                    safe = self.client.validate_stop_side(d, cand, mark)[0]
                    if safe:
                        first = lad.get("ts") is None
                        lad["ts"] = float(cand)
                        hub_state.save(self.hub)
                        self._log(f"Trailing Stop ({t}) {'takes charge' if first else 'moves'} at {cand} "
                                  f"on the candle close.")
                    elif t in (hub.TRAIL_LLHH, hub.TRAIL_CANDLE):
                        # The price is already through the new level (lowest low / highest high, or the candle
                        # level): it is broken.
                        self._log(f"Trailing level {cand} is already crossed by the price (mark {mark}) - closing now.")
                        await self._close_position(reason="trailing_stop")
                        return True
                    # Moving average / Supertrend line on the wrong side of the price: it does not take over (the stop in
                    # charge stays).
        return await self._hub_ladder_check(mark)

    async def _hub_cancel_stop_child(self) -> None:
        """A stop-limit that already triggered left a LIMIT order on the exchange (not tagged with the bot's own
        prefix): it is cancelled by its id when the trade ends."""
        lad = self.hub.ladder or {}
        ex = lad.get("ex") or {}
        if ex.get("kind") != "limit" or not ex.get("algo_id"):
            return
        try:
            o = await self.client.get_algo_order(ex["algo_id"])
        except Exception as e:
            self._log(f"Could not read the stop-limit {ex['algo_id']} ({e}).")
            return
        child = o.get("actualOrderId") if isinstance(o, dict) else None
        if not child:
            return
        try:
            child_id = int(child)
        except (TypeError, ValueError):
            child_id = child
        try:
            await self.client.cancel_regular_order(self.symbol, child_id)
            self._log(f"Cancelled the limit order {child_id} left by the triggered stop-limit.")
        except Exception as e:
            self._log(f"The limit order {child_id} of the triggered stop-limit was not cancelled ({e}) - it may "
                      f"already be filled or gone.")

    # ------------------------------------------------------------------ status
    # ------------------------------------------------------------------ SHADOW (owner rule 2026-10-24)
    # The shadow shadows the real trade all the way: a PAPER copy of the whole plan (app/shadow_book.py) that runs
    # all the time while Shadow mode is picked - EXCEPT that it does NOT go into pause when the actual trade goes to
    # pause (the trades-off period, Stop on the pair, STOP ALL). It never sends an order.
    # The trades-off rule is unchanged: a real winning trade that makes a new tracker high starts it; the next N
    # trades are off (counted on the shadow); then real trading goes live again.
    def _hub_shadow_on(self) -> bool:
        return bool(self._is_hub() and not self._plan_error and (self._plan.get("shadow") or {}).get("on"))

    def _hub_book(self) -> dict:
        if self.hub.shadow_book is None:
            self.hub.shadow_book = shadow_book.new_book(self._plan["shadow"]["tracker_start"])
            sh = self.tracker.get_shadow()
            if sh.pos or sh.current:        # a paper trade of the older shadow engine: the book starts fresh
                sh.pos, sh.current = 0, None
                self.tracker.set_shadow(sh)
                tracker_mod.save(self.tracker)
            hub_state.save(self.hub)
        return self.hub.shadow_book

    def _hub_book_open(self) -> bool:
        return bool(self._hub_shadow_on() and self.hub.shadow_book and self.hub.shadow_book.get("pos"))

    async def _hub_shadow_tick(self, snap) -> None:
        """One closed candle of the shadow (paper only, nothing is sent to the exchange)."""
        if not self._hub_shadow_on():
            return
        book = self._hub_book()
        try:
            tick = (await self.client.get_symbol_info(self.symbol)).price_tick or 0.0
        except Exception as e:
            tick = 0.0
            self._log(f"Could not read the price tick for paper slippage ({e}) - using 0.")
        # The balance is needed only to size a paper entry by the fixed base amount or to work out a $ max loss.
        balance = None
        sz = self._plan["sizing"]
        if hub.uses_fixed_base(self._plan) or (sz["stop_on"] and sz.get("stop_basis") == hub.STOP_USD):
            try:
                balance = await self.client.get_available_balance()
            except Exception as e:
                self._log(f"Could not read the available balance for the paper size ({e}).")
        events, closed = shadow_book.on_candle(book, snap, self._plan, tick_size=tick, last_total=self.hub.last_total,
                                               balance=balance, t_ms=int(time.time() * 1000))
        for ev in events:
            self._log(f"[SHADOW] {ev}")
        await self._hub_shadow_closed(closed, candle_time=snap.open_time)
        hub_state.save(self.hub)

    async def _hub_shadow_live(self, price: float) -> None:
        """The live price for the open paper trade (stops, trailing, targets)."""
        if not self._hub_book_open():
            return
        book = self.hub.shadow_book
        before = (book["pos"], book["frac"], book["levels"].get("ts"), sum(1 for t in book["tps"] if t["state"] != "pending"))
        closed = shadow_book.on_price(book, price, self._plan, int(time.time() * 1000))
        for rec in closed:
            self._log(f"[SHADOW] paper {rec['direction']} closed ({rec['exit_reason']}) "
                      f"result {rec['result_pct']:+.3f}%")
        if closed:
            await self._hub_shadow_closed(closed, candle_time=None)
        after = (book["pos"], book["frac"], book["levels"].get("ts"), sum(1 for t in book["tps"] if t["state"] != "pending"))
        if closed or before != after:
            hub_state.save(self.hub)

    async def _hub_shadow_closed(self, records: list, candle_time) -> None:
        """Every closed paper trade goes to the Tracker tab's paper list. While trades are off, it counts: after the
        number of trades you set, real trading goes live again (a flip at the candle close blocks the real entry on
        that same candle, as before)."""
        sh = self.tracker.get_shadow()
        n_wanted = int(self._plan["shadow"]["trades"])
        ended = False
        for rec in records:
            row = dict(rec)
            row["during_trades_off"] = bool(sh.active)
            sh.history.append(row)
            if len(sh.history) > tracker_mod.MAX_HISTORY:
                del sh.history[: len(sh.history) - tracker_mod.MAX_HISTORY]
            if sh.active:
                sh.count += 1
                sh.trades_total += 1
                if sh.count >= n_wanted:
                    sh.active = False
                    by_flip = "FLIP" in str(rec.get("exit_reason") or "")
                    sh.resume_block_time = candle_time if (by_flip and candle_time is not None) else None
                    sh.last_end_reason = f"{n_wanted} trade(s) off done"
                    ended = True
        cur = (self.hub.shadow_book or {}).get("current")
        sh.current = dict(cur) if cur else None
        self.tracker.set_shadow(sh)
        tracker_mod.save(self.tracker)
        if ended:
            self._log(f"[SHADOW] trades off done ({n_wanted}) - real trading goes live again.")
            await self._notify(tg.notify_info(
                self.account_name, self.symbol,
                f"Trades off ended after {n_wanted} shadow trade(s). Real trading goes live again.",
                self.pc.telegram_enabled, email_kind="Shadow Ended"))

    async def _tick_shadow_only(self) -> None:
        """The pair is STOPPED (no real trading) but the shadow keeps running: candles and the live price only.
        Nothing real is read for trading, placed, changed or closed."""
        if not self._hub_shadow_on():
            return
        plan = self._plan
        frame, snap = await self._hub_fetch(plan)
        if snap is None:
            return
        if self._hub_is_new_candle(snap):
            self._hub_mark_candle(snap)
            await self._hub_shadow_tick(snap)
        if self._hub_book_open() and self._hub_live_price() is None:
            await self._hub_shadow_live(await self.client.get_mark_price(self.symbol))

    def _hub_status(self) -> dict | None:
        if not self._is_hub():
            return None
        return {
            "plan_summary": hub.plan_summary(self._plan),
            "wait": dict(self.hub.wait),
            "open_now": self.hub.open_now,
            "first_trade_done": self.hub.first_trade_done,
            "last_processed_candle": self.hub.last_processed_candle,
            "max_loss_on": self._plan["sizing"]["stop_on"],
            "tp": self.hub.tp,
            "trail_on": self._plan["trail"]["on"],
            "trail_type": self._plan["trail"].get("type"),
            "stop_loss_on": bool(self._plan["sl"]["on"]),
            "ladder": self.hub.ladder,
            "reentry_block": self.hub.reentry_block,
            "counter_sizing_on": bool((self._plan["sizing"].get("counter") or {}).get("on")),
            "last_total": self.hub.last_total,          # fixed base: the total the next entry is built on
            "shadow_on": bool((self._plan.get("shadow") or {}).get("on")),
            "shadow_only": bool(getattr(self, "_shadow_only", False)),
            "shadow_equity": (self.hub.shadow_book or {}).get("equity"),
            "shadow_position": ((self.hub.shadow_book or {}).get("current") if self.hub.shadow_book else None),
        }
