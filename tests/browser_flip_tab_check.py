"""
browser_flip_tab_check.py - OPTIONAL real-browser check (2026-10-25): the flip tab's 3 Yes / No boxes (Same as entry,
Same as hold rule, Same as entry wait), and "Indicator Combination". Backend = the REAL plan routes; no exchange calls.
Run:  python tests/browser_flip_tab_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_flip_tab")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_ma1={"flip": True}, name="Old flip plan")   # saved before the flip tab
PLAN1.pop("flip_rules", None)
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



def only(page, fid, value):
    """Condition ticks (2026-10-25): tick exactly this one condition (real clicks)."""
    for x in page.query_selector_all(f"#hub_{fid} input[type=checkbox]"):
        want = x.get_attribute("value") == value
        if x.is_checked() != want:
            x.click()
    page.wait_for_timeout(120)


def ticked(page, fid):
    return page.eval_on_selector_all(f"#hub_{fid} input[type=checkbox]", "xs => xs.filter(x => x.checked).map(x => x.value)")


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def wait(ms=200): page.wait_for_timeout(ms)
    def vis(i): return page.is_visible("#hub_" + i)
    def plan_of(sym): return store.get_account(acc.id).pairs[sym].plan
    def openfn(fn):
        wait(60)
        if page.is_visible("#dlg-hub-remind"): page.click("#hub-remind-ok"); wait(60)
        page.click(f'.hubx-item[data-fn="{fn}"]'); wait(150)
    def flipcol_visible():
        return page.evaluate("getComputedStyle(document.querySelector('#hub_ma1_flip')).visibility") == "visible"
    def save():
        page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        names = page.eval_on_selector_all(".hubx-iname", "els => els.map(e => e.firstChild.textContent)")
        check("menu says Indicator Combination (not Combine)", "Indicator Combination" in names and "Combine" not in names, str(names))
        page.click("#hub-confirm-btn"); wait(200)
        check("a new form starts with all 3 = Yes", all(page.is_checked("#hub_" + x) for x in ("flip_same_entry", "flip_same_hold", "flip_same_wait")))
        openfn("ind")
        check("Same as entry Yes: FLIP column hidden", not flipcol_visible())
        page.select_option("#hub-pair-select", "BBBUSDT") if False else None
        openfn("pair"); page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        openfn("flipact")
        check("an older plan loads: Same as entry = No, hold and wait = Yes",
              not page.is_checked("#hub_flip_same_entry") and page.is_checked("#hub_flip_same_hold") and page.is_checked("#hub_flip_same_wait"))
        openfn("ind")
        check("Same as entry No: FLIP column shown, MA 1 FLIP ticked", flipcol_visible() and page.is_checked("#hub_ma1_flip"))
        check("Main for FLIP shown", vis("main_flip"))
        openfn("combine")
        check("Indicator Combination for FLIP shown", vis("combine_flip"))
        openfn("flipact")
        page.check("#hub_flip_same_entry"); wait(120)
        openfn("combine")
        check("Same as entry Yes: Indicator Combination for FLIP hidden", not vis("combine_flip"))
        openfn("ind")
        check("Same as entry Yes: Main for FLIP hidden", not vis("main_flip"))
        openfn("flipact")
        check("own hold / own wait boxes hidden while Yes", not vis("flip_hold_on") and not vis("flip_wait"))
        page.uncheck("#hub_flip_same_hold"); page.uncheck("#hub_flip_same_wait"); wait(120)
        check("No shows the flip's own hold and wait", vis("flip_hold_on") and vis("flip_wait"))
        page.check("#hub_flip_hold_on"); wait(100)
        check("own hold ON shows its ADX level", vis("flip_hold_level"))
        page.fill("#hub_flip_hold_level", "25"); page.fill("#hub_flip_wait", "0")
        openfn("ind")
        check("own hold uses ADX: its numbers open", page.is_visible("#hubcard_adx"))
        page.fill("#hub_adx_di_length", "14"); page.fill("#hub_adx_smoothing", "14"); page.fill("#hub_adx_level", "20")
        save()
        fr = (plan_of("AAAUSDT") or {}).get("flip_rules") or {}
        check("saved: same entry Yes, own hold 25, own wait 0",
              (fr.get("same_entry"), fr.get("same_hold"), fr.get("hold_on"), fr.get("hold_level"), fr.get("same_wait"), fr.get("wait"))
              == (True, False, True, 25, False, 0), str(fr) + page.inner_text("#hub-msg")[:200])
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review: FLIP Same as entry, own hold, own wait", "FLIP: Same as entry" in rv and "own hold rule (ADX below 25)" in rv and "own wait 0" in rv, rv[:700])
        if page.is_visible("#dlg-hub-review"): page.click("#dlg-hub-review button.ghost"); wait(100)
        openfn("flipact")
        for key in ("flip_same_entry", "flip_same_hold", "flip_hold_on", "flip_hold_level", "flip_same_wait", "flip_wait"):
            el = page.query_selector(f'.info-i[data-help="{key}"]')
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
