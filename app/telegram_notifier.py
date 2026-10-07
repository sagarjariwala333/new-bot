"""
telegram_notifier.py
=====================

Best-effort push notifications for every trade lifecycle event, across every
account/pair instance. Modeled directly on the TelegramNotifier pattern from
the existing bot: bot token + chat id come from environment variables ONLY
(never stored in config.json / the accounts store, never returned by any API
response), so the dashboard cannot leak credentials even though it has no
login of its own. `telegram_enabled` is the one dashboard-editable knob per
pair - a pure mute/unmute switch.

A failed or unconfigured notification NEVER raises - this must not be able
to crash a trading loop. Worst case, you just don't get a message.

BUG FIX (2026-09-15, found during a telegram-notifier review, owner-
approved): every message is sent with parse_mode="HTML", but the DYNAMIC
parts (error text, account names, symbols) were interpolated into the
message RAW, unescaped. Telegram's HTML parser rejects the ENTIRE message
if it contains a "<" or "&" that isn't part of a real, recognized tag -
and real exception text very plausibly contains exactly that (e.g. a
ConnectionError's string form often includes literal "<...>" segments).
This meant the alerts most likely to matter (sustained failures, exactly
the kind this session's escalation work was built for) were also the
messages most likely to silently fail to send - rejected by Telegram with
a 400, logged, but never actually reaching the operator's phone. Every
notify_* function below now escapes its dynamic arguments with
html.escape() before building the message - the literal formatting tags
(<b>, etc.) are still written by this file itself, never escaped, since
those are real, intended tags.

CONTINGENCY (2026-09-15, owner request): if a Telegram send fails for ANY
reason (network error, bad token, OR the HTML-parse rejection above, in
case some other unescaped edge case is ever found later), this now falls
back to sending a plain-text email via email_notifier.py - so a Telegram
outage doesn't mean total silence. Each notify_* function below builds its
OWN clean, data-only email body (account/symbol/price/SL/TP/time - no
emoji, no HTML, no prose) rather than just stripping tags from the richer
Telegram message - the owner asked for the email specifically to be
simple data, not a plain-text copy of the chat message. The email subject
is "Trade-Bot - <kind>" (Entry/Exit/SL Update/Error/etc.) so the type of
alert is visible from an inbox list without opening it.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time

import aiohttp

from app.email_notifier import email_notifier

log = logging.getLogger("telegram")


def _now() -> str:
    # Matches the timestamp format already used in instance.py's _log /
    # ledger.py, for consistency across every part of this project that
    # shows a human a point in time.
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _strip_html_for_email(message: str) -> str:
    """Fallback ONLY for a caller that didn't supply a dedicated
    email_body (kept for backward compatibility / anything that ever
    calls notifier.send() directly without going through a notify_*
    helper) - strips this file's own simple tags (<b>, etc.) rather than
    showing them literally in a plain-text email."""
    return re.sub(r"<[^>]+>", "", message)


class TelegramNotifier:
    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, message: str, enabled: bool = True,
                    email_kind: str = "Alert", email_body: str | None = None):
        """email_kind becomes the email fallback's subject ("Trade-Bot -
        <email_kind>"); email_body is the clean, data-only email content -
        if omitted, falls back to a stripped-tags copy of `message` for
        any caller that hasn't been updated to supply one."""
        if not enabled:
            return
        body_for_email = email_body if email_body is not None else _strip_html_for_email(message)
        subject_for_email = f"Trade-Bot - {email_kind}"
        if not self.configured:
            # Not configured at all - the email fallback is still worth
            # trying here, since "Telegram screwed up" includes "Telegram
            # was never set up" for a system depending on this for alerts.
            await email_notifier.send(subject_for_email, body_for_email)
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        log.warning("Telegram send failed (%s): %s", resp.status, body)
                        await email_notifier.send(subject_for_email, body_for_email)
        except Exception as e:
            log.warning("Telegram notification failed (bot keeps running regardless): %s", e)
            await email_notifier.send(subject_for_email, body_for_email)


# Single shared notifier instance - one bot/chat for the whole system.
# (If you eventually want a different chat per account, swap this for a
# dict keyed by account_id and read TELEGRAM_CHAT_ID_<ACCOUNT_ID> per account.)
notifier = TelegramNotifier()


def tag(account_name: str, symbol: str) -> str:
    return f"<b>[{html.escape(account_name)} · {html.escape(symbol)}]</b>"


async def notify_entry(account_name: str, symbol: str, direction: str, qty: float,
                        entry_price: float, sl: float, tp1: float | None, tp2: float | None,
                        leverage: float, enabled: bool, alloc_pct: float | None = None):
    """BASE V3 entry message: one FIXED stop order; TP1/TP2 are tracking
    levels only (never orders)."""
    arrow = "🟢" if direction == "LONG" else "🔴"
    alloc_str = f"{alloc_pct:g}% of balance" if alloc_pct is not None else "—"
    msg = (
        f"{arrow} {tag(account_name, symbol)} New <b>{html.escape(direction)}</b> position opened\n"
        f"Qty: {qty}\n"
        f"Entry: {entry_price}\n"
        f"Stop (fixed): {sl}\n"
        f"TP1 tracker: {tp1}\n"
        f"TP2 tracker: {tp2}\n"
        f"Allocation: {alloc_str}\n"
        f"Leverage: {leverage}x"
    )
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Qty: {qty}\n"
        f"Entry: {entry_price}\n"
        f"Stop (fixed): {sl}\n"
        f"TP1 tracker: {tp1}\n"
        f"TP2 tracker: {tp2}\n"
        f"Allocation: {alloc_str}\n"
        f"Leverage: {leverage}x\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Entry", email_body=email_body)


async def notify_info(account_name: str, symbol: str, message: str, enabled: bool,
                      email_kind: str = "Info"):
    """Informational (non-error) notice - e.g. shadow mode started/ended."""
    msg = f"ℹ️ {tag(account_name, symbol)} {html.escape(message)}"
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"{message}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind=email_kind, email_body=email_body)


async def notify_exit(account_name: str, symbol: str, direction: str, reason: str,
                       exit_price: float, pnl: float | None, enabled: bool):
    arrow = "✅" if (pnl or 0) >= 0 else "⛔"
    pnl_str = f"{pnl:.4f} USDT" if pnl is not None else "—"
    msg = (
        f"{arrow} {tag(account_name, symbol)} <b>{html.escape(direction)}</b> position closed "
        f"({html.escape(reason)})\n"
        f"Exit: {exit_price}\n"
        f"PnL: {pnl_str}"
    )
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Reason: {reason}\n"
        f"Exit: {exit_price}\n"
        f"PnL: {pnl_str}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Exit", email_body=email_body)


async def notify_sl_update(account_name: str, symbol: str, direction: str, new_sl: float,
                            trailing: bool, enabled: bool):
    label = "Trailing stop updated" if trailing else "Stop-loss updated"
    msg = f"🔧 {tag(account_name, symbol)} {label} for {html.escape(direction)} → {new_sl}"
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"{'Trailing SL' if trailing else 'SL'}: {new_sl}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled,
                         email_kind="Trailing Update" if trailing else "SL Update",
                         email_body=email_body)


async def notify_error(account_name: str, symbol: str, error: str, enabled: bool):
    msg = f"⚠️ {tag(account_name, symbol)} Error: {html.escape(error)}"
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Error: {error}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Error", email_body=email_body)


async def notify_started(account_name: str, symbol: str, timeframe: str, enabled: bool):
    msg = f"🤖 {tag(account_name, symbol)} Bot started ({html.escape(timeframe)} timeframe)."
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Started", email_body=email_body)


async def notify_withdraw_alert(account_name: str, equity: float, threshold: float, enabled: bool):
    """Account-wide equity threshold crossed - matches the reference Pine
    script's alertcondition() for the withdrawal trigger. Notification only:
    no auto-withdrawal - any transfer is a manual action for the account
    owner, not something this bot does automatically."""
    msg = (
        f"💰 <b>[{html.escape(account_name)}]</b> Equity threshold hit: {equity:,.2f} USDT "
        f"crossed {threshold:,.2f} USDT.\n"
        f"Manual action per your own rule: review your positions and any transfer to "
        f"your Spot wallet."
    )
    email_body = (
        f"Account: {account_name}\n"
        f"Equity: {equity:,.2f} USDT\n"
        f"Threshold: {threshold:,.2f} USDT\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Withdraw Alert", email_body=email_body)


async def notify_stopped(account_name: str, symbol: str, enabled: bool):
    msg = f"🛑 {tag(account_name, symbol)} Bot stopped."
    email_body = f"Account: {account_name}\nSymbol: {symbol}\nTime: {_now()}"
    await notifier.send(msg, enabled, email_kind="Stopped", email_body=email_body)


async def notify_resumed(account_name: str, symbol: str, direction: str, enabled: bool):
    msg = f"🔄 {tag(account_name, symbol)} Resumed - found an existing open {html.escape(direction)} position on startup."
    email_body = (
        f"Account: {account_name}\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Time: {_now()}"
    )
    await notifier.send(msg, enabled, email_kind="Resumed", email_body=email_body)
