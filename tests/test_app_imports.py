"""
test_app_imports.py
====================

Found during a thorough re-check (2026-09-14): app/main.py and
app/api/__init__.py - the actual FastAPI application and every one of its
routes - had NEVER been import-verified by this test suite, in this
session or any prior one. Every other test file only imports app.auth,
app.instance, app.strategy, etc. directly; nothing ever imported app.main
or app.api, so a broken import there (an undefined name, a wrong decorator,
a missing dependency) could have shipped unnoticed indefinitely, with only
ast.parse (which catches syntax errors, not import-time errors) standing
between it and a real deployment.

Closed by extending the existing fastapi/aiohttp stub pattern with a new
pydantic stub (none of the three real packages are installed in this
project's own dev sandbox - confirmed no PyPI access either, so building
stubs was the only option) - enough to actually import both files and
verify every route got registered, not just that the file parses.

2026-09-14 correction (same day): the original version of this file
unpacked app.main.app.routes as (path, func, kwargs) TUPLES - matching
this project's own test stub's internal representation at the time, but
NOT real FastAPI, whose .routes is a list of Route/APIRoute OBJECTS
exposing .path/.endpoint/.dependencies attributes, not tuples. Two
independent third-party reviews, run with real fastapi actually
installed, caught this: the test passed here purely by coincidence
(matching the stub it happened to be written against) while it would
raise TypeError against the real library it was supposed to be
verifying - exactly the kind of gap this whole file exists to close.
Fixed by rewriting this file to use attribute access, and by fixing the
stub itself (_fastapi_stub.py) to expose the same attributes real
FastAPI does, so the identical test code is now correct against both.

Deliberately NOT claimed: that pydantic's real field validation is
correct (min_length, ge/le, etc. are accepted by the stub but not
enforced - see _pydantic_stub.py's own docstring) - only that the code is
structurally sound at import time. Real validation still needs a real
pydantic environment to verify.
"""

import os
import shutil
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

TEST_DATA_DIR = "/tmp/hull_bot_test_app_imports"
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = TEST_DATA_DIR
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

import app.main  # noqa: E402
import app.api  # noqa: E402


class TestAppModulesImportCleanly(unittest.TestCase):
    """The core check: these two imports succeeding at all (no
    ModuleNotFoundError, AttributeError, NameError, etc. at import time)
    is itself the main thing this file proves - it genuinely could not be
    proven before this stub existed."""

    def test_expected_routes_are_registered(self):
        registered_paths = {route.path for route in app.main.app.routes}
        expected = {
            "/auth/login", "/auth/logout", "/auth/status", "/auth/change-password",
            "/accounts", "/accounts/{account_id}",
            "/accounts/{account_id}/pairs", "/accounts/{account_id}/pairs/{symbol}",
            "/accounts/{account_id}/pairs/{symbol}/start",
            "/accounts/{account_id}/pairs/{symbol}/stop",
            "/accounts/{account_id}/pairs/{symbol}/restart",
            "/status", "/status/{account_id}/{symbol}",
            "/ledger/{account_id}/{symbol}", "/ledger/{account_id}/{symbol}/csv",
            "/analysis/{account_id}/{symbol}/walk-forward",
            "/analysis/{account_id}/{symbol}/monte-carlo",
            "/", "/healthz",
        }
        missing = expected - registered_paths
        self.assertFalse(missing, f"expected routes missing from the registered app: {missing}")

    def test_healthz_route_does_not_require_auth(self):
        """Deliberately unauthenticated (see main.py's own comment on this) -
        confirm no auth/csrf dependency got attached to it."""
        for route in app.main.app.routes:
            if route.path == "/healthz":
                self.assertEqual(route.dependencies, [], "/healthz must stay reachable without login")
                return
        self.fail("/healthz route not found")

    def test_healthz_counts_both_platforms_not_just_binance(self):
        """2026-09-16 fix (item #8): running_instances used to count ONLY
        manager.instances (Binance) - an account running entirely on OKX
        would report 0 even while genuinely healthy and trading. Confirms
        the total now sums both, with the per-platform breakdown also
        present."""
        fake_binance_manager = mock.Mock()
        fake_binance_manager.instances = {"a": 1, "b": 2}
        fake_okx_manager = mock.Mock()
        fake_okx_manager.instances = {"c": 1, "d": 2, "e": 3}

        with mock.patch.object(app.main, "manager", fake_binance_manager), \
             mock.patch.object(app.main, "okx_manager", fake_okx_manager):
            response = app.main.healthz()

        body = response.content
        self.assertEqual(body["running_instances"], 5, "must be the TOTAL across both platforms")
        self.assertEqual(body["running_instances_binance"], 2)
        self.assertEqual(body["running_instances_okx"], 3)

    def test_healthz_correct_with_only_okx_running_and_zero_binance(self):
        """The exact scenario the bug report described - an account
        running entirely on OKX must not report 0 running instances."""
        fake_binance_manager = mock.Mock()
        fake_binance_manager.instances = {}
        fake_okx_manager = mock.Mock()
        fake_okx_manager.instances = {"a": 1}

        with mock.patch.object(app.main, "manager", fake_binance_manager), \
             mock.patch.object(app.main, "okx_manager", fake_okx_manager):
            response = app.main.healthz()

        body = response.content
        self.assertEqual(body["running_instances"], 1,
                         "an OKX-only account must not report 0 running instances")

    def test_mutating_account_routes_require_csrf(self):
        """Spot-check: a state-changing route (create account) must carry
        the mutating_deps (auth + CSRF), not be reachable unauthenticated."""
        for route in app.api.router.routes:
            if route.path == "/accounts" and route.endpoint.__name__ == "create_account":
                self.assertTrue(route.dependencies,
                                "a state-changing route must have auth/CSRF dependencies attached")
                return
        self.fail("POST /accounts route not found")

    def test_global_control_routes_are_registered_and_csrf_protected(self):
        """2026-09-15: the STOP ALL / START ALL routes - irreversible-ish
        (real positions, real capital) and cross-platform, so this checks
        both exist AND both carry the same auth/CSRF protection as every
        other mutating route."""
        found = {"stop_all": False, "start_all": False}
        for route in app.api.router.routes:
            if route.endpoint.__name__ in found:
                found[route.endpoint.__name__] = True
                self.assertTrue(route.dependencies,
                                f"{route.endpoint.__name__} must have auth/CSRF dependencies attached")
        self.assertTrue(all(found.values()), f"missing routes: {[k for k, v in found.items() if not v]}")

    def test_forgot_and_reset_password_routes_are_registered_and_public(self):
        """2026-09-15: these two are deliberately PUBLIC (no session exists
        yet at this point - same security model as /auth/login itself),
        so this checks the opposite of the CSRF test above: they must NOT
        carry auth/CSRF dependencies, or a locked-out owner could never
        reach them in the first place."""
        found = {"forgot_password": False, "reset_password": False}
        for route in app.api.router.routes:
            if route.endpoint.__name__ in found:
                found[route.endpoint.__name__] = True
                self.assertFalse(route.dependencies,
                                 f"{route.endpoint.__name__} must be public - it's the recovery path "
                                 f"for when the owner can't log in at all")
        self.assertTrue(all(found.values()), f"missing routes: {[k for k, v in found.items() if not v]}")


if __name__ == "__main__":
    unittest.main()
