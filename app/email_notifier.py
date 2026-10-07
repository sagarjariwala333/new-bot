"""
email_notifier.py
===================

Contingency notification channel (2026-09-15, owner request): "in case
Telegram screws up." This is NOT a duplicate of every Telegram message -
it only fires when telegram_notifier.py's send actually fails (bad token,
network unreachable, Telegram's own API rejecting the message) or when
Telegram was never configured at all. See telegram_notifier.py's own
docstring for exactly where this gets called from.

Configured via environment variables ONLY (same principle as Telegram's
bot token/chat id - never stored in the accounts store, never returned by
any API response):
  SMTP_HOST          - e.g. smtp.gmail.com
  SMTP_PORT          - default 587 (STARTTLS)
  SMTP_USERNAME       - the account used to authenticate/send
  SMTP_PASSWORD       - an app password, not your real account password,
                        for providers that support one (e.g. Gmail)
  SMTP_FROM_EMAIL     - defaults to SMTP_USERNAME if not set separately
  ALERT_EMAIL_TO      - OPTIONAL. The owner normally types the alert email
                        into the dashboard (Default Settings page), where
                        it is stored privately on the server. If the
                        dashboard field is blank, this variable is used.
                        If both are blank, no email is sent.

A failed or unconfigured email send NEVER raises, same principle as
Telegram - this must not be able to crash a trading loop. If BOTH
Telegram and email fail, the failure is still logged, which is the last
line of defense.

smtplib is synchronous/blocking - run inside asyncio.to_thread so a slow
or hanging SMTP connection can never stall the trading loop.
"""

from __future__ import annotations

import asyncio
import email.mime.text
import logging
import os
import smtplib

log = logging.getLogger("email_notifier")


class EmailNotifier:
    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587") or "587")
        self.username = os.environ.get("SMTP_USERNAME", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_addr = os.environ.get("SMTP_FROM_EMAIL", "") or self.username

    _to_addr_override: str | None = None   # explicit programmatic override (used by tests)

    @property
    def to_addr(self) -> str:
        """Destination address: the dashboard value first, then ALERT_EMAIL_TO,
        otherwise blank (nothing is sent). Read on every send so a change on
        the dashboard takes effect immediately."""
        if self._to_addr_override is not None:
            return self._to_addr_override
        from app.default_settings import default_settings
        return default_settings.get_alert_email() or os.environ.get("ALERT_EMAIL_TO", "")

    @to_addr.setter
    def to_addr(self, value: str) -> None:
        self._to_addr_override = value

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.to_addr)

    def _send_sync(self, subject: str, body: str):
        msg = email.mime.text.MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        with smtplib.SMTP(self.host, self.port, timeout=10) as server:
            server.starttls()
            server.login(self.username, self.password)
            server.sendmail(self.from_addr, [self.to_addr], msg.as_string())

    async def send(self, subject: str, body: str):
        if not self.configured:
            return  # not configured - silently skip, don't spam the log every failed Telegram send
        try:
            await asyncio.to_thread(self._send_sync, subject, body)
        except Exception as e:
            log.warning("Email fallback notification failed (bot keeps running regardless): %s", e)


# Single shared notifier instance, matching telegram_notifier.py's own pattern.
email_notifier = EmailNotifier()
