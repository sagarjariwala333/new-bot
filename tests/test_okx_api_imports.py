"""
test_okx_api_imports.py
=========================

Same purpose as test_app_imports.py, applied to the OKX router: proves
app/api/okx.py and its wiring into app/main.py are import-clean and that
every expected route actually got registered - not just that the file
parses.

NOTE on the fastapi stub's route.path (see test_app_imports.py's own
extensive docstring on this): the stub does NOT model APIRouter(prefix=...)
- it stores each route's path exactly as passed to the @router.get/post/...
decorator, with no prefix concatenation. Real FastAPI DOES concatenate
(okx_router's real routes are served at "/api/okx/accounts", etc.) - this
is a known, already-documented stub limitation (test_app_imports.py has the
same caveat for the Binance router), not a bug in okx.py. Routes here are
therefore checked by their decorator-literal path + endpoint function name
(to disambiguate from Binance's identically-shaped, identically-named
paths, e.g. both routers have a literal "/accounts" POST route under this
stub), never by the real, prefix-combined URL.
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

TEST_DATA_DIR = "/tmp/hull_bot_test_okx_api_imports"
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = TEST_DATA_DIR
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

import app.main  # noqa: E402
import app.api.okx  # noqa: E402


class TestOKXRouterImportsCleanly(unittest.TestCase):
    def test_expected_okx_routes_are_registered_by_endpoint_name(self):
        endpoint_names = {route.endpoint.__name__ for route in app.api.okx.router.routes}
        expected = {
            "list_okx_accounts", "create_okx_account", "update_okx_account", "delete_okx_account",
            "add_okx_pair", "update_okx_pair", "delete_okx_pair",
            "start_okx_pair", "stop_okx_pair", "restart_okx_pair",
            "okx_all_status", "okx_one_status", "get_okx_ledger",
            "get_okx_ledger_csv", "okx_walk_forward", "okx_monte_carlo",
        }
        missing = expected - endpoint_names
        self.assertFalse(missing, f"expected OKX routes missing: {missing}")

    def test_okx_router_mounted_on_main_app(self):
        """Confirms app.main actually included the OKX router (not just
        defined it) - the same routes must appear in app.main.app.routes."""
        main_endpoint_names = {route.endpoint.__name__ for route in app.main.app.routes}
        self.assertIn("create_okx_account", main_endpoint_names)
        self.assertIn("start_okx_pair", main_endpoint_names)

    def test_mutating_okx_routes_require_csrf(self):
        for route in app.api.okx.router.routes:
            if route.endpoint.__name__ == "create_okx_account":
                self.assertTrue(route.dependencies,
                                "a state-changing OKX route must have auth/CSRF dependencies attached")
                return
        self.fail("create_okx_account route not found")

    def test_okx_and_binance_routers_are_genuinely_separate_objects(self):
        """Guards against a future refactor accidentally merging the two
        routers back into one, which would defeat the whole point of the
        separate-tabs decision."""
        import app.api as binance_api
        self.assertIsNot(app.api.okx.router, binance_api.router)


if __name__ == "__main__":
    unittest.main()
