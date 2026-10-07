"""
browser_default_settings_check.py
=================================

OPTIONAL real-browser check (needs Playwright + Chromium; not part of the unittest
suite). It opens the real dashboard.html in Chromium and drives the new
"Default Settings" tab and the Add Pair dialogs.

Backend: a tiny local HTTP server that answers the dashboard's requests by calling
the REAL route functions in app/api (the same code the app uses) - only
fastapi / pydantic / aiohttp are the project's test stand-ins. Exchange calls are
never made. Uses fake accounts and placeholder numbers from tests/_test_values.py.

Run:  python tests/browser_default_settings_check.py
"""

import json
import os
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_browser_defaults")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

import app.default_settings as ds  # noqa: E402
import app.store as store_module  # noqa: E402
import app.okx_store as okx_store_module  # noqa: E402
import app.api as api  # noqa: E402
import app.api.okx as okx_api  # noqa: E402
from app.api.schemas import PairCreate, OKXPairCreate  # noqa: E402
from tests import _test_values as tv  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"

store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
store = store_module.Store()
okx_store = okx_store_module.OKXStore()
api.store = store
okx_api.okx_store = okx_store
ds.default_settings = ds.DefaultSettingsStore(path=TEST_DATA_DIR / "default_settings.json")
acc = store.create_account("Test", "k", "s", True)
okx_acc = okx_store.create_account("Demo", "k", "s", "p", demo=True)


class Body:
    def __init__(self, d):
        self.__dict__.update(d)


def route(method, path, payload):
    """Maps the dashboard's requests onto the real route functions."""
    try:
        if path == "/api/auth/status":
            return 200, {"ok": True}
        if path == "/api/accounts":
            return 200, api.list_accounts()
        if path == "/api/okx/accounts":
            return 200, okx_api.list_okx_accounts()
        if path in ("/api/status", "/api/okx/status"):
            return 200, {"instances": [], "startup_failures": {}}
        if path == "/healthz":
            return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/default-settings" and method == "GET":
            return 200, api.get_default_settings()
        if path.startswith("/api/default-settings/") and method == "PUT":
            return 200, api.save_default_settings(path.rsplit("/", 1)[1], Body(payload))
        if path.startswith("/api/default-settings/") and path.endswith("/confirm"):
            return 200, api.confirm_default_settings(path.split("/")[3])
        if path == "/api/alert-email":
            return 200, api.save_alert_email(Body(payload))
        if path == "/api/live-trading":
            return 200, api.set_live_trading(Body(payload))
        if path == f"/api/accounts/{acc.id}/pairs" and method == "POST":
            return 200, api.add_pair(acc.id, PairCreate(**payload))
        if path == f"/api/okx/accounts/{okx_acc.id}/pairs" and method == "POST":
            return 200, okx_api.add_okx_pair(okx_acc.id, OKXPairCreate(**payload))
        return 404, {"detail": f"unhandled {method} {path}"}
    except Exception as e:  # HTTPException from the real routes
        return getattr(e, "status_code", 500), {"detail": getattr(e, "detail", str(e))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        if self.path == "/" and method == "GET":
            return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        code, body = route(method, self.path.split("?")[0], payload)
        self._send(code, body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    results = []

    def confirm_btn(i):
        return page.locator("#tabpage-defaults button", has_text="Confirm Default Settings").nth(i)

    def save_btn(i):
        return page.locator("#tabpage-defaults button", has_text=re.compile(r"^Save$")).nth(i)

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        page.goto(base)
        page.wait_for_timeout(600)

        # ---- the page loads without script errors; old branding is gone
        check("page has no JavaScript errors on load", not errors, str(errors))

        # ---- Add Pair is refused until defaults are confirmed
        page.click("#btn-add-pair")
        page.wait_for_timeout(300)
        check("Add Pair before Confirm shows a 'not confirmed' warning", any("not confirmed" in m for m in dialogs), str(dialogs))
        check("Add Pair dialog did NOT open", not page.is_visible("#dlg-pair"))

        # ---- Default Settings tab: everything blank
        page.click("#maintab-defaults")
        page.wait_for_timeout(400)
        check("Default Settings tab is shown", page.is_visible("#tabpage-defaults"))
        vals = page.eval_on_selector_all("#ds-fields-binance input, #ds-fields-binance select", "els => els.map(e => e.value)")
        check("every Binance default field starts blank", len(vals) > 20 and all(v == "" for v in vals), f"{len(vals)} fields, non-blank: {[v for v in vals if v != '']}")
        okx_vals = page.eval_on_selector_all("#ds-fields-okx input, #ds-fields-okx select", "els => els.map(e => e.value)")
        check("every OKX default field starts blank (incl. trigger type)", len(okx_vals) == len(vals) + 1 and all(v == "" for v in okx_vals))
        check("Binance badge says NOT CONFIRMED", "NOT CONFIRMED" in page.inner_text("#ds-badge-binance"))
        check("live trading switch starts OFF", not page.is_checked("#ds-live"))

        # ---- Confirm with blanks is refused
        confirm_btn(0).click()
        page.wait_for_timeout(500)
        msg = page.inner_text("#ds-msg-binance")
        check("Confirm with blank fields is refused and names blank fields", "still blank" in msg and "hma_length" in msg, msg[:120])
        check("review dialog did NOT open", not page.is_visible("#dlg-ds-review"))

        # ---- fill every Binance field, save, then confirm via the review dialog
        def fill_platform(platform):
            for key, value in {**tv.STRATEGY_VALUES, **tv.PAIR_EXTRA}.items():
                sel = f"#ds-{platform}-{key}"
                tag = page.eval_on_selector(sel, "e => e.tagName")
                if tag == "SELECT":
                    page.select_option(sel, str(value).lower() if isinstance(value, bool) else str(value))
                else:
                    page.fill(sel, str(value))
            if platform == "okx":
                page.select_option("#ds-okx-trigger_px_type", "mark")

        fill_platform("binance")
        save_btn(0).click()
        page.wait_for_timeout(500)
        check("Save works and says NOT confirmed yet", "NOT confirmed yet" in page.inner_text("#ds-msg-binance"), page.inner_text("#ds-msg-binance"))
        confirm_btn(0).click()
        page.wait_for_timeout(500)
        check("review dialog opens with the entered values", page.is_visible("#dlg-ds-review") and str(tv.STRATEGY_VALUES["hma_length"]) in page.inner_text("#ds-review-body"))
        page.click("#ds-review-apply")
        page.wait_for_timeout(500)
        check("badge now says CONFIRMED", "CONFIRMED" in page.inner_text("#ds-badge-binance") and "NOT" not in page.inner_text("#ds-badge-binance"))

        # ---- Add Pair now opens, pre-filled from the confirmed defaults, and saves
        page.click("#maintab-trading")
        page.wait_for_timeout(200)
        dialogs.clear()
        page.click("#btn-add-pair")
        page.wait_for_timeout(500)
        check("Add Pair dialog opens after confirming", page.is_visible("#dlg-pair"), str(dialogs))
        check("symbol box starts empty (no pre-filled coin)", page.input_value("#pair-symbol") == "")
        check("pair form is pre-filled from the confirmed defaults", page.input_value("#f_hma_length") == str(tv.STRATEGY_VALUES["hma_length"]))
        page.fill("#pair-symbol", "TESTUSDT")
        page.click("#pair-save")
        page.wait_for_timeout(600)
        pairs = store.get_account(acc.id).pairs
        check("pair was created on the server from those values", "TESTUSDT" in pairs and pairs["TESTUSDT"].leverage == tv.STRATEGY_VALUES["leverage"])
        check("creating a pair did not start it", pairs["TESTUSDT"].enabled is False)

        # ---- editing a default un-confirms it (and existing pairs are untouched)
        page.click("#maintab-defaults")
        page.wait_for_timeout(300)
        page.fill("#ds-binance-hma_length", "12")
        save_btn(0).click()
        page.wait_for_timeout(500)
        check("editing a default marks it NOT CONFIRMED again", "NOT CONFIRMED" in page.inner_text("#ds-badge-binance"))
        check("existing pair kept its own values", store.get_account(acc.id).pairs["TESTUSDT"].hma_length == tv.STRATEGY_VALUES["hma_length"])

        # ---- an out-of-range value is rejected by the server and shown
        page.fill("#ds-binance-leverage", "2.5")
        save_btn(0).click()
        page.wait_for_timeout(500)
        check("a bad value is rejected with a message", "whole number" in page.inner_text("#ds-msg-binance"), page.inner_text("#ds-msg-binance"))

        # ---- alert email
        page.fill("#ds-email", "not-an-email")
        page.click("#ds-email-save")
        page.wait_for_timeout(400)
        check("invalid email is rejected", "valid email" in page.inner_text("#ds-email-msg"), page.inner_text("#ds-email-msg"))
        page.fill("#ds-email", "me@example.com")
        page.click("#ds-email-save")
        page.wait_for_timeout(400)
        check("valid email saves on the server", ds.default_settings.get_alert_email() == "me@example.com")

        # ---- live-trading switch (browser confirm accepted by the handler above)
        page.check("#ds-live")
        page.wait_for_timeout(400)
        check("live switch turns ON on the server after a confirm prompt", ds.default_settings.live_trading_enabled() and any("live trading" in m.lower() for m in dialogs))
        page.uncheck("#ds-live")
        page.wait_for_timeout(400)
        check("live switch turns OFF again", not ds.default_settings.live_trading_enabled())

        # ---- OKX tab: Add Pair is refused until its own defaults are confirmed
        page.click("#maintab-okx")
        page.wait_for_timeout(500)
        dialogs.clear()
        page.click("#btn-okx-add-pair")
        page.wait_for_timeout(300)
        check("OKX Add Pair refused until OKX defaults are confirmed", any("OKX" in m and "not confirmed" in m for m in dialogs), str(dialogs))
        page.click("#maintab-defaults")
        page.wait_for_timeout(300)
        fill_platform("okx")
        confirm_btn(1).click()
        page.wait_for_timeout(500)
        page.click("#ds-review-apply")
        page.wait_for_timeout(500)
        check("OKX defaults confirm", ds.default_settings.is_confirmed("okx"))
        page.click("#maintab-okx")
        page.wait_for_timeout(300)
        page.click("#btn-okx-add-pair")
        page.wait_for_timeout(500)
        check("OKX Add Pair opens, trigger type pre-filled from defaults", page.is_visible("#dlg-okx-pair") and page.input_value("#okx-pair-trigger-type") == "mark")

        # ---- analysis forms start blank (close the open OKX dialog first - a modal blocks the tab bar)
        page.evaluate("document.getElementById('dlg-okx-pair').close()")
        page.click("#maintab-analysis")
        blanks = page.eval_on_selector_all("#analysis-wf input, #analysis-mc input", "els => els.map(e => e.value)")
        check("analysis inputs all start blank", len(blanks) == 10 and all(v == "" for v in blanks), str(blanks))

        check("no JavaScript errors during the whole session", not errors, str(errors))
        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
