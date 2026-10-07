import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._fastapi_stub  # noqa: F401,E402  (only used if fastapi isn't really installed - guards HTTPException)

TEST_DATA_DIR = "/tmp/hull_bot_test_csrf"
os.environ["DATA_DIR"] = TEST_DATA_DIR

from fastapi import HTTPException  # noqa: E402
from app.auth import require_csrf, issue_csrf_token, CSRF_COOKIE  # noqa: E402


class FakeRequest:
    """require_csrf() only ever touches .cookies (dict-like), .method (str),
    and .headers (dict-like) - a plain duck-typed stand-in avoids depending
    on fastapi/Starlette's real Request constructor, which takes a raw ASGI
    `scope` dict, not keyword args, and would break this test the moment the
    real fastapi package (rather than any local stub) is installed."""
    def __init__(self, cookies=None, method="GET", headers=None):
        self.cookies = cookies or {}
        self.method = method
        self.headers = headers or {}


class TestCSRF(unittest.TestCase):
    def test_get_requests_are_never_checked(self):
        req = FakeRequest(cookies={}, method="GET", headers={})
        require_csrf(req)  # must not raise, regardless of missing cookie/header

    def test_post_without_cookie_or_header_rejected(self):
        req = FakeRequest(cookies={}, method="POST", headers={})
        with self.assertRaises(HTTPException) as ctx:
            require_csrf(req)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_post_with_matching_cookie_and_header_allowed(self):
        token = issue_csrf_token()
        req = FakeRequest(cookies={CSRF_COOKIE: token}, method="POST",
                          headers={"x-csrf-token": token})
        require_csrf(req)  # must not raise

    def test_post_with_mismatched_cookie_and_header_rejected(self):
        req = FakeRequest(cookies={CSRF_COOKIE: "aaa"}, method="POST",
                          headers={"x-csrf-token": "bbb"})
        with self.assertRaises(HTTPException) as ctx:
            require_csrf(req)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_delete_and_put_are_also_checked(self):
        for method in ("PUT", "DELETE", "PATCH"):
            req = FakeRequest(cookies={}, method=method, headers={})
            with self.assertRaises(HTTPException):
                require_csrf(req)


if __name__ == "__main__":
    unittest.main()
