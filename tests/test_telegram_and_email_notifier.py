import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

import app.telegram_notifier as tg  # noqa: E402
from app.email_notifier import EmailNotifier  # noqa: E402


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "some error body"


class _FakeSession:
    def __init__(self, status):
        self.status = status
        self.posted_with = None

    def post(self, url, data=None, timeout=None):
        self.posted_with = data
        return _FakeResponse(self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestHTMLEscaping(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the fix: dynamic content (error text, account
    names, symbols) used to be interpolated RAW into an HTML-parsed
    message - an unescaped '<' or '&' makes Telegram reject the ENTIRE
    message, silently losing exactly the alerts most likely to matter."""

    def setUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"
        os.environ["TELEGRAM_CHAT_ID"] = "fake_chat"
        tg.notifier.bot_token = "fake_token"
        tg.notifier.chat_id = "fake_chat"
        self.sent_messages = []

        async def spy_send(message, enabled=True, **kwargs):
            self.sent_messages.append(message)
        self._original_send = tg.notifier.send
        tg.notifier.send = spy_send

    def tearDown(self):
        tg.notifier.send = self._original_send

    async def test_error_text_with_angle_brackets_is_escaped(self):
        await tg.notify_error("Main", "BTCUSDT",
                               "ConnectionError: <urlopen error [Errno 111]>", True)
        msg = self.sent_messages[0]
        self.assertIn("&lt;urlopen error", msg)
        self.assertNotIn("<urlopen", msg)

    async def test_error_text_with_ampersand_is_escaped(self):
        await tg.notify_error("Main", "BTCUSDT", "fetch failed & retry exhausted", True)
        msg = self.sent_messages[0]
        self.assertIn("&amp;", msg)

    async def test_literal_formatting_tags_survive_escaping(self):
        """The <b> tags THIS FILE writes must remain real tags, not get
        escaped themselves - only the dynamic arguments are escaped."""
        await tg.notify_error("Main", "BTCUSDT", "a plain error", True)
        msg = self.sent_messages[0]
        self.assertIn("<b>", msg)

    async def test_account_name_with_special_characters_is_escaped(self):
        await tg.notify_started("<script>Main</script>", "BTCUSDT", "4h", True)
        msg = self.sent_messages[0]
        self.assertNotIn("<script>Main</script>", msg)
        self.assertIn("&lt;script&gt;", msg)


class TestEmailSubjectAndCleanBody(unittest.IsolatedAsyncioTestCase):
    """2026-09-15 owner request: email subject should reflect the
    notification type ("Trade-Bot - Entry", etc.), and the body should be
    simple, data-only content - not a stripped copy of the richer,
    emoji/HTML-formatted Telegram message."""

    def setUp(self):
        tg.notifier.bot_token = ""  # unconfigured - forces the email path every time
        tg.notifier.chat_id = ""
        self.email_calls = []

        async def fake_email_send(subject, body):
            self.email_calls.append((subject, body))
        self._original_email_send = tg.email_notifier.send
        tg.email_notifier.send = fake_email_send

    def tearDown(self):
        tg.email_notifier.send = self._original_email_send

    async def test_entry_email_subject_and_body(self):
        await tg.notify_entry("Main", "BTCUSDT", "LONG", 0.05, 63000.0, 61500.0, 63500.0, 64500.0,
                              6.0, True, alloc_pct=13.0)
        subject, body = self.email_calls[0]
        self.assertEqual(subject, "Trade-Bot - Entry")
        self.assertIn("Symbol: BTCUSDT", body)
        self.assertIn("Entry: 63000.0", body)
        self.assertIn("Stop (fixed): 61500.0", body)
        self.assertIn("TP1 tracker: 63500.0", body)
        self.assertIn("TP2 tracker: 64500.0", body)
        self.assertIn("Allocation: 13% of balance", body)
        self.assertIn("Leverage: 6.0x", body)
        self.assertIn("Time:", body)
        self.assertNotIn("<b>", body)  # no HTML/emoji flavor text in the email body
        self.assertNotIn("🟢", body)

    async def test_exit_email_subject_and_body(self):
        await tg.notify_exit("Main", "BTCUSDT", "LONG", "force_close_ema", 62000.0, 15.25, True)
        subject, body = self.email_calls[0]
        self.assertEqual(subject, "Trade-Bot - Exit")
        self.assertIn("Reason: force_close_ema", body)
        self.assertIn("Exit: 62000.0", body)
        self.assertIn("PnL: 15.2500 USDT", body)

    async def test_sl_update_email_subject(self):
        await tg.notify_sl_update("Main", "BTCUSDT", "LONG", 62200.0, trailing=False, enabled=True)
        subject, body = self.email_calls[0]
        self.assertEqual(subject, "Trade-Bot - SL Update")
        self.assertIn("SL: 62200.0", body)

    async def test_trailing_update_email_subject(self):
        await tg.notify_sl_update("Main", "BTCUSDT", "LONG", 62800.0, trailing=True, enabled=True)
        subject, body = self.email_calls[0]
        self.assertEqual(subject, "Trade-Bot - Trailing Update")

    async def test_error_email_subject(self):
        await tg.notify_error("Main", "BTCUSDT", "connection reset", True)
        subject, body = self.email_calls[0]
        self.assertEqual(subject, "Trade-Bot - Error")
        self.assertIn("Error: connection reset", body)

    async def test_no_email_address_is_built_in(self):
        """The package ships with NO destination address: with nothing on the
        dashboard and nothing in the environment, the address is blank and the
        notifier reports itself as not configured."""
        from app.email_notifier import EmailNotifier
        from app.default_settings import default_settings
        os.environ.pop("ALERT_EMAIL_TO", None)
        default_settings.set_alert_email("")
        n = EmailNotifier()
        n.host, n.username, n.password = "smtp.example.com", "u", "p"
        self.assertEqual(n.to_addr, "")
        self.assertFalse(n.configured)

    async def test_dashboard_email_is_used_and_beats_the_environment(self):
        from app.email_notifier import EmailNotifier
        from app.default_settings import default_settings
        os.environ["ALERT_EMAIL_TO"] = "env@example.com"
        try:
            n = EmailNotifier()
            self.assertEqual(n.to_addr, "env@example.com")
            default_settings.set_alert_email("dash@example.com")
            self.assertEqual(n.to_addr, "dash@example.com")   # takes effect immediately, no restart
        finally:
            default_settings.set_alert_email("")
            os.environ.pop("ALERT_EMAIL_TO", None)


class TestTelegramFallsBackToEmailOnFailure(unittest.IsolatedAsyncioTestCase):
    """The actual wiring: a failed Telegram send (bad status, network
    error, or simply unconfigured) must trigger the email fallback -
    but a SUCCESSFUL send must never also send a redundant email."""

    def setUp(self):
        tg.notifier.bot_token = "fake_token"
        tg.notifier.chat_id = "fake_chat"
        self.email_calls = []

        async def fake_email_send(subject, body):
            self.email_calls.append((subject, body))
        self._original_email_send = tg.email_notifier.send
        tg.email_notifier.send = fake_email_send

    def tearDown(self):
        tg.email_notifier.send = self._original_email_send

    async def test_successful_telegram_send_does_not_trigger_email(self):
        fake_session = _FakeSession(status=200)
        with mock.patch("app.telegram_notifier.aiohttp.ClientSession", return_value=fake_session):
            await tg.notifier.send("hello", enabled=True)
        self.assertEqual(self.email_calls, [])

    async def test_telegram_400_response_triggers_email_fallback(self):
        fake_session = _FakeSession(status=400)
        with mock.patch("app.telegram_notifier.aiohttp.ClientSession", return_value=fake_session):
            await tg.notifier.send("hello", enabled=True)
        self.assertEqual(len(self.email_calls), 1)
        self.assertIn("hello", self.email_calls[0][1])

    async def test_telegram_network_exception_triggers_email_fallback(self):
        def raising_session():
            raise ConnectionError("network down")
        with mock.patch("app.telegram_notifier.aiohttp.ClientSession", side_effect=raising_session):
            await tg.notifier.send("hello", enabled=True)
        self.assertEqual(len(self.email_calls), 1)

    async def test_unconfigured_telegram_still_triggers_email(self):
        tg.notifier.bot_token = ""
        tg.notifier.chat_id = ""
        await tg.notifier.send("hello", enabled=True)
        self.assertEqual(len(self.email_calls), 1)

    async def test_disabled_notification_sends_neither_telegram_nor_email(self):
        await tg.notifier.send("hello", enabled=False)
        self.assertEqual(self.email_calls, [])

    async def test_email_body_has_html_tags_stripped(self):
        fake_session = _FakeSession(status=400)
        with mock.patch("app.telegram_notifier.aiohttp.ClientSession", return_value=fake_session):
            await tg.notifier.send("<b>[Main]</b> Error: bad stuff", enabled=True)
        body = self.email_calls[0][1]
        self.assertNotIn("<b>", body)
        self.assertIn("[Main]", body)
        self.assertIn("Error: bad stuff", body)


class TestEmailNotifier(unittest.IsolatedAsyncioTestCase):
    def test_not_configured_when_fields_missing(self):
        n = EmailNotifier()
        n.host = ""
        self.assertFalse(n.configured)

    def test_configured_when_all_fields_present(self):
        n = EmailNotifier()
        n.host, n.username, n.password, n.to_addr = "smtp.example.com", "u", "p", "me@example.com"
        self.assertTrue(n.configured)

    async def test_unconfigured_send_does_not_attempt_smtp(self):
        n = EmailNotifier()
        n.host = ""
        with mock.patch("smtplib.SMTP") as fake_smtp:
            await n.send("subject", "body")
            fake_smtp.assert_not_called()

    async def test_configured_send_calls_smtp_with_correct_args(self):
        n = EmailNotifier()
        n.host, n.port = "smtp.example.com", 587
        n.username, n.password = "user@example.com", "app-password"
        n.from_addr, n.to_addr = "user@example.com", "me@example.com"

        fake_server = mock.MagicMock()
        fake_smtp_cm = mock.MagicMock()
        fake_smtp_cm.__enter__ = mock.Mock(return_value=fake_server)
        fake_smtp_cm.__exit__ = mock.Mock(return_value=False)

        with mock.patch("smtplib.SMTP", return_value=fake_smtp_cm) as fake_smtp_ctor:
            await n.send("Test Subject", "Test body")

        fake_smtp_ctor.assert_called_once_with("smtp.example.com", 587, timeout=10)
        fake_server.starttls.assert_called_once()
        fake_server.login.assert_called_once_with("user@example.com", "app-password")
        fake_server.sendmail.assert_called_once()

    async def test_smtp_failure_never_raises(self):
        n = EmailNotifier()
        n.host, n.username, n.password, n.to_addr = "smtp.example.com", "u", "p", "me@example.com"
        with mock.patch("smtplib.SMTP", side_effect=ConnectionError("smtp down")):
            await n.send("subject", "body")  # must not raise


if __name__ == "__main__":
    unittest.main()
