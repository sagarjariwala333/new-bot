"""
browser_new_options_check.py - OPTIONAL real-browser check (2026-11-11): counter-trend running total choice, flip choice
(Reverse / Close only), Ignore late signals. Backend = the REAL plan routes; no exchange calls.
Run:  python tests/browser_new_options_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_new_options")
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
PLAN1 = make_plan(ind_ma1={"entry": True, "flip": True}, name="Old fixed plan",
                  sizing={"method": "Fixed base amount (grows with the last closed total)", "base_amount": 80.0, "leverage": 15,
                          "counter": {"on": True, "by": "ma1", "base_amount": 40.0, "leverage": 15}})
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



def apply_review(page):
    """Confirm needs a Start-as choice when that box is shown (2026-11-09): pick Follow, then confirm."""
    page.wait_for_timeout(150)
    if page.is_visible("#hub-startas-box") and not page.is_checked("#startas-follow") and not page.is_checked("#startas-new"):
        page.click("#startas-follow"); page.wait_for_timeout(120)
    page.click("#hub-review-apply")


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
    def save():
        page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            apply_review(page); wait(800)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; warns = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: warns.append(m.text) if "No help text" in m.text else None)
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        # ---- a NEW form
        openfn("flipact")
        check("new form: flip choice starts as Reverse", page.input_value("#hub_flip_choice") == L_REV)
        names = page.eval_on_selector_all("#hub_flip_choice option", "os => os.map(o => o.value)")
        check("flip choice offers Reverse and Close only", len(names) == 3 and names[2].startswith("Close only"), str(names))
        openfn("size")
        check("new form: counter total hidden until Fixed + counter are chosen", not vis("counter_total"))
        # ---- the OLD fixed plan (no total key)
        openfn("pair"); page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        openfn("size")
        check("old plan: counter total is shown and = One total shared", vis("counter_total") and page.input_value("#hub_counter_total") == "shared",
              page.input_value("#hub_counter_total") if vis("counter_total") else "hidden")
        openfn("flipact")
        check("old plan: flip choice = Reverse", page.input_value("#hub_flip_choice") == L_REV)
        save()
        pl = plan_of("AAAUSDT") or {}
        check("old plan saved again: NO total key and NO flip_action change (stays as it always was)",
              "total" not in pl["sizing"]["counter"] and pl.get("flip_action") in (None, L_REV) and "late_skip" not in pl, str(pl.get("sizing")) + page.inner_text("#hub-msg")[:200])
        # ---- change all three
        openfn("size"); page.select_option("#hub_counter_total", "separate"); wait(100)
        openfn("flipact"); page.select_option("#hub_flip_choice", L_CLOSE); wait(100)
        page.click("#hub-edit-btn"); wait(100)
        check("Ignore late signals is not picked on an older plan", page.get_attribute('#hub-menu button[data-fn="latesig"]', "aria-pressed") != "true")
        page.click('#hub-menu button[data-fn="latesig"]'); wait(60)
        page.click("#hub-confirm-btn"); wait(200)
        openfn("latesig")
        check("minutes shown once picked", vis("late_min"))
        page.fill("#hub_late_min", "30")
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review shows all three", "own running total" in rv and "enter only when the full entry rules agree" in rv and "more than 30 minute" in rv, rv[-900:])
        if page.is_visible("#dlg-hub-review"):
            apply_review(page); wait(800)
        pl = plan_of("AAAUSDT") or {}
        check("saved: total separate, flip Close only, late 30",
              pl["sizing"]["counter"].get("total") == "separate" and pl.get("flip_action") == L_CLOSE and pl.get("late_skip") == {"on": True, "minutes": 30}, str(pl))
        # ---- reload
        page.reload(); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        openfn("pair"); page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        openfn("size"); check("reloaded: total separate", page.input_value("#hub_counter_total") == "separate")
        openfn("flipact"); check("reloaded: flip Close only", page.input_value("#hub_flip_choice") == L_CLOSE)
        openfn("latesig"); check("reloaded: late signals 30", page.input_value("#hub_late_min") == "30")
        # ---- info icons
        for fn, key in (("size", "counter_total"), ("flipact", "flip_choice"), ("latesig", "late_min")):
            openfn(fn)
            el = page.query_selector(f'.info-i[data-help="{key}"]')
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        check("no 'No help text' warning", not warns, str(warns))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)


L_REV = "Reverse (close + open opposite)"
L_CLOSE = "Close only, then wait for the entry rules"

if __name__ == "__main__":
    main()
