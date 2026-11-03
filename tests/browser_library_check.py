"""
browser_library_check.py - OPTIONAL real-browser check of the STRATEGY LIBRARY (2026-10-24): Save to library,
the Library list, Load onto another pair, automatic History when a pair's strategy is changed or removed, Remove
plan from pair, Delete with a confirm (Playwright + Chromium).
Backend = the REAL plan, library and remove-plan routes of app/api; no exchange calls.
Run:  python tests/browser_library_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_library")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

import tests._aiohttp_stub  # noqa
import tests._pydantic_stub  # noqa
import tests._fastapi_stub  # noqa
import app.store as store_module
import app.okx_store as okx_store_module
import app.api as api
import app.api.okx as okx_api
from app import strategy_library as L
from tests import _test_values as tv
from tests._hub_plan import make_plan

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
L.LIBRARY_FILE = TEST_DATA_DIR / "strategy_library.json"
store = store_module.Store(); okx_store = okx_store_module.OKXStore()
api.store = store; okx_api.okx_store = okx_store


class _Mgr:
    def get(self, *a): return None
    async def restart(self, *a): pass


api.manager = _Mgr()
acc = store.create_account("Test", "k", "s", True)
PLAN1 = make_plan(ind_ma1={"entry": True}, entry_wait=2, name="Trend A")
store.add_pair(acc.id, tv.pair_config("AAAUSDT", plan=PLAN1, timeframe="4h"))
store.add_pair(acc.id, tv.pair_config("BBBUSDT", plan=None, timeframe="1h"))


class Body:
    def __init__(self, d): self.__dict__.update(d)


def route(method, path, payload):
    try:
        if path == "/api/auth/status": return 200, {"ok": True}
        if path == "/api/accounts": return 200, api.list_accounts()
        if path == "/api/okx/accounts": return 200, okx_api.list_okx_accounts()
        if path in ("/api/status", "/api/okx/status"): return 200, {"instances": [], "startup_failures": {}}
        if path == "/healthz": return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/default-settings": return 200, api.get_default_settings()
        if path == "/api/library" and method == "GET": return 200, asyncio.run(api.get_library())
        if path == "/api/library" and method == "POST": return 200, asyncio.run(api.save_to_library(Body(payload)))
        m = re.fullmatch(r"/api/library/([0-9a-f]+)", path)
        if m and method == "DELETE": return 200, asyncio.run(api.delete_from_library(m.group(1)))
        m = re.fullmatch(rf"/api/accounts/{acc.id}/pairs/([A-Z]+)(/plan|/start)?", path)
        if m:
            sym, tail = m.group(1), m.group(2) or ""
            if tail == "/plan" and method == "GET": return 200, asyncio.run(api.get_pair_plan(acc.id, sym))
            if tail == "/plan" and method == "PUT": return 200, asyncio.run(api.set_pair_plan(acc.id, sym, Body(payload)))
            if tail == "/plan" and method == "DELETE": return 200, asyncio.run(api.remove_pair_plan(acc.id, sym))
            if tail == "" and method == "PUT": store.update_pair(acc.id, sym, payload); return 200, {}
        return 404, {"detail": f"unhandled {method} {path}"}
    except Exception as e:
        return getattr(e, "status_code", 500), {"detail": getattr(e, "detail", str(e))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self, method):
        if self.path == "/" and method == "GET":
            raw, code, ct = DASHBOARD.read_bytes(), 200, "text/html; charset=utf-8"
        else:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            code, body = route(method, self.path.split("?")[0], payload)
            raw, ct = json.dumps(body).encode(), "application/json"
        self.send_response(code); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self): self._go("GET")
    def do_PUT(self): self._go("PUT")
    def do_POST(self): self._go("POST")
    def do_DELETE(self): self._go("DELETE")


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def wait(ms=200): page.wait_for_timeout(ms)
    def dismiss():
        wait(80)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); wait(60)
    def plan_of(sym): return store.get_account(acc.id).pairs[sym].plan
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        check("Library button is on the Strategy Hub", page.is_visible("#hub-lib-open-btn"))
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        check("the saved plan of AAAUSDT is loaded", page.input_value("#hub_name") == "Trend A" and page.input_value("#hub_timeframe") == "4h")
        check("Save to library and Remove plan buttons shown", page.is_visible("#hub-lib-save-btn") and page.is_visible("#hub-remove-btn"))
        # ---- Save to library
        page.click("#hub-lib-save-btn"); wait()
        check("Save to library asks for the name (filled from the strategy name)", page.is_visible("#dlg-lib-save") and page.input_value("#lib-save-name") == "Trend A")
        page.fill("#lib-save-name", ""); page.click("#lib-save-ok"); wait()
        check("an empty name is refused", "name" in page.inner_text("#lib-save-msg") and not L.LIBRARY_FILE.exists())
        page.fill("#lib-save-name", "BTC trend proven"); page.click("#lib-save-ok"); wait(500)
        saved = L.list_all()["saved"]
        check("saved in the library with pair, account, timeframe", len(saved) == 1 and saved[0]["name"] == "BTC trend proven"
              and saved[0]["symbol"] == "AAAUSDT" and saved[0]["timeframe"] == "4h" and saved[0]["account_name"] == "Test", str(saved)[:300])
        check("the pair itself is not changed", plan_of("AAAUSDT") == PLAN1)
        check("message with SGT time", "SGT" in page.inner_text("#hub-msg"), page.inner_text("#hub-msg"))
        # ---- Library list
        page.click("#hub-lib-open-btn"); wait(400)
        txt = page.inner_text("#lib-saved")
        check("library lists it with date (SGT) and where from", "BTC trend proven" in txt and "SGT" in txt and "AAAUSDT" in txt and "4h" in txt, txt[:300])
        check("history is empty so far", "No old versions yet" in page.inner_text("#lib-history"))
        page.click("#dlg-library button.ghost:has-text('Close')"); wait()
        # ---- Load onto another pair
        page.select_option("#hub-pair-select", "BBBUSDT"); wait(500)
        check("BBBUSDT has no plan", "No plan saved" in page.inner_text("#hub-state"))
        page.click("#hub-lib-open-btn"); wait(400)
        page.click("#lib-saved .lib-load"); wait()
        check("Load asks to confirm, names the pair", page.is_visible("#dlg-lib-confirm") and "BBBUSDT" in page.inner_text("#lib-confirm-body"))
        page.click("#lib-confirm-ok"); wait(400); dismiss()
        check("loaded into the form: name, timeframe, entry wait", page.input_value("#hub_name") == "BTC trend proven"
              and page.input_value("#hub_timeframe") == "4h" and page.input_value("#hub_entry_wait") == "2",
              f'{page.input_value("#hub_name")} {page.input_value("#hub_timeframe")} {page.input_value("#hub_entry_wait")}')
        check("Load did NOT save anything on the pair", plan_of("BBBUSDT") is None)
        check("load message says nothing is saved yet", "Nothing is saved on the pair yet" in page.inner_text("#hub-msg"))
        page.click("#hub-save-btn"); wait(300)
        page.click("#hub-review-apply"); wait(800)
        p2 = plan_of("BBBUSDT") or {}
        check("Save puts it on BBBUSDT with the timeframe", p2.get("entry_wait") == 2 and p2.get("name") == "BTC trend proven"
              and store.get_account(acc.id).pairs["BBBUSDT"].timeframe == "4h", page.inner_text("#hub-msg")[:200])
        check("first plan on a pair adds no history", L.list_all()["history"] == [])
        # ---- change it -> the old version goes to History
        page.fill("#hub_name", "BTC trend v2"); page.click("#hub-save-btn"); wait(300)
        page.click("#hub-review-apply"); wait(800)
        h = L.list_all()["history"]
        check("changing the plan keeps the old one in History", len(h) == 1 and h[0]["name"] == "BTC trend proven" and h[0]["note"] == "changed"
              and h[0]["symbol"] == "BBBUSDT", str(h)[:300])
        # ---- Remove plan from pair
        page.click("#hub-remove-btn"); wait()
        check("Remove asks to confirm", page.is_visible("#dlg-lib-confirm") and "History" in page.inner_text("#lib-confirm-body"))
        page.click("#lib-confirm-ok"); wait(700)
        check("plan removed from the pair", plan_of("BBBUSDT") is None, page.inner_text("#hub-msg"))
        h = L.list_all()["history"]
        check("removed plan kept in History", len(h) == 2 and h[0]["name"] == "BTC trend v2" and h[0]["note"] == "removed")
        check("state line says no plan", "No plan saved" in page.inner_text("#hub-state"))
        page.click("#hub-remove-btn"); wait(); page.click("#lib-confirm-ok"); wait(500)
        check("removing again is refused with a message", page.inner_text("#hub-msg").startswith("Not removed"), page.inner_text("#hub-msg"))
        # ---- library shows history, delete with confirm
        page.click("#hub-lib-open-btn"); wait(400)
        ht = page.inner_text("#lib-history")
        check("History shows both with what happened", "Removed from this pair" in ht and "Replaced on this pair" in ht, ht[:300])
        page.click("#lib-history details summary"); wait()
        check("Details show the full summary lines", "ENTRY" in page.inner_text("#lib-history details"))
        page.click("#lib-saved .lib-del"); wait()
        check("Delete asks to confirm", page.is_visible("#dlg-lib-confirm") and "cannot be undone" in page.inner_text("#lib-confirm-body"))
        page.click("#dlg-lib-confirm button.ghost"); wait()
        check("Cancel deletes nothing", len(L.list_all()["saved"]) == 1)
        page.click("#lib-saved .lib-del"); wait(); page.click("#lib-confirm-ok"); wait(500)
        check("Delete removes it after confirm", L.list_all()["saved"] == [] and "Nothing saved yet" in page.inner_text("#lib-saved"))
        check("History untouched by that delete", len(L.list_all()["history"]) == 2)
        # ---- info icons
        page.click("#dlg-library button.ghost:has-text('Close')"); wait()
        for key in ("lib.open", "lib.save", "lib.remove"):
            el = page.query_selector(f'.info-i[data-help="{key}"]')
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        # ---- phone
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
