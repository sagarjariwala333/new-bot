import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._fastapi_stub  # noqa: F401,E402  (registers a stub only if fastapi isn't really installed)

TEST_DATA_DIR = Path("/tmp/hull_bot_test_auth")
os.environ["DASHBOARD_USERNAME"] = "admin"
os.environ["DASHBOARD_PASSWORD"] = "testpass123"

import app.auth as auth_module  # noqa: E402


def fresh_store():
    """See the identical note in test_store.py/test_ledger.py: AUTH_FILE is
    only read from the DATA_DIR env var once, at first import, for the whole
    test process - overriding the module attribute directly (not the env
    var) is what actually isolates each test."""
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    auth_module.AUTH_FILE = TEST_DATA_DIR / "auth.json"
    return auth_module.AuthStore()


class TestLogin(unittest.TestCase):
    def test_correct_credentials_issue_a_token(self):
        store = fresh_store()
        token = store.attempt_login("admin", "testpass123")
        self.assertIsNotNone(token)
        self.assertTrue(store.validate(token))

    def test_wrong_password_rejected(self):
        store = fresh_store()
        token = store.attempt_login("admin", "wrongpassword")
        self.assertIsNone(token)

    def test_wrong_username_rejected(self):
        store = fresh_store()
        token = store.attempt_login("notadmin", "testpass123")
        self.assertIsNone(token)

    def test_garbage_token_never_validates(self):
        store = fresh_store()
        self.assertFalse(store.validate("not-a-real-token"))
        self.assertFalse(store.validate(None))


class TestLockout(unittest.TestCase):
    def test_five_failed_attempts_locks_out_the_sixth(self):
        from fastapi import HTTPException
        store = fresh_store()
        for _ in range(5):
            self.assertIsNone(store.attempt_login("admin", "wrong"))
        with self.assertRaises(HTTPException) as ctx:
            store.attempt_login("admin", "testpass123")  # even the CORRECT password, while locked out
        self.assertEqual(ctx.exception.status_code, 429)


class TestChangePassword(unittest.TestCase):
    def test_wrong_current_password_rejected(self):
        store = fresh_store()
        self.assertFalse(store.change_password("wrongcurrent", "newpass456"))

    def test_correct_current_password_changes_it(self):
        store = fresh_store()
        self.assertTrue(store.change_password("testpass123", "newpass456"))
        self.assertIsNotNone(store.attempt_login("admin", "newpass456"))
        self.assertIsNone(store.attempt_login("admin", "testpass123"))

    def test_persists_across_reload(self):
        store = fresh_store()
        store.change_password("testpass123", "newpass456")
        store2 = auth_module.AuthStore()  # same (overridden) AUTH_FILE path, still in effect
        self.assertIsNotNone(store2.attempt_login("admin", "newpass456"))


class TestForgotPassword(unittest.TestCase):
    """2026-09-15, owner request: password reset via email, before this
    there was no recovery path short of server-level access."""

    def test_correct_username_issues_a_token(self):
        store = fresh_store()
        token = store.request_password_reset("admin")
        self.assertIsNotNone(token)
        self.assertGreater(len(token), 20)  # a real random token, not a short/guessable one

    def test_wrong_username_returns_none(self):
        store = fresh_store()
        token = store.request_password_reset("not-the-real-admin")
        self.assertIsNone(token)

    def test_second_request_within_cooldown_returns_none(self):
        """Prevents spamming the recovery inbox - not a second valid token
        per request, one per cooldown window."""
        store = fresh_store()
        first = store.request_password_reset("admin")
        second = store.request_password_reset("admin")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_valid_token_resets_the_password(self):
        store = fresh_store()
        token = store.request_password_reset("admin")
        ok = store.reset_password_with_token(token, "brandnewpass789")
        self.assertTrue(ok)
        self.assertIsNotNone(store.attempt_login("admin", "brandnewpass789"))
        self.assertIsNone(store.attempt_login("admin", "testpass123"))

    def test_token_is_single_use(self):
        store = fresh_store()
        token = store.request_password_reset("admin")
        store.reset_password_with_token(token, "firstnewpass")
        second_attempt = store.reset_password_with_token(token, "secondnewpass")
        self.assertFalse(second_attempt, "a used token must not work a second time")

    def test_unknown_token_is_rejected(self):
        store = fresh_store()
        self.assertFalse(store.reset_password_with_token("totally-made-up-token", "somepass123"))

    def test_expired_token_is_rejected(self):
        store = fresh_store()
        token = store.request_password_reset("admin")
        # Simulate time passing past the token's TTL, without a real sleep.
        for t in store._reset_tokens:
            store._reset_tokens[t] = 0.0  # force-expire it
        self.assertFalse(store.reset_password_with_token(token, "somepass123"))

    def test_successful_reset_clears_any_existing_lockout(self):
        store = fresh_store()
        for _ in range(auth_module.MAX_FAILED_ATTEMPTS):
            store.attempt_login("admin", "wrongpassword")
        locked, _ = store.is_locked_out("admin")
        self.assertTrue(locked, "should be locked out after repeated failures - test setup check")

        token = store.request_password_reset("admin")
        store.reset_password_with_token(token, "brandnewpass789")

        locked_after, _ = store.is_locked_out("admin")
        self.assertFalse(locked_after, "a successful reset must clear the lockout")
        self.assertIsNotNone(store.attempt_login("admin", "brandnewpass789"))


if __name__ == "__main__":
    unittest.main()

