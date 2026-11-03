"""
browser_break_even_check.py - OPTIONAL real-browser check (2026-10-26): Break-Even tab - After TP (1, 2, 3) and
After ATR (100 = 1x ATR), each with its tick box, the review line and the saved plan.
Backend = the REAL plan routes; no exchange calls.  Run:  python tests/browser_break_even_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_break_even")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_ma1={"flip": True}, name="Plan before Break-Even")
PLAN1.pop("be", None)
PLAN1["tp"] = {"on": True, "basis": "% of price movement", "atr_length": 14,
               "levels": [{"on": True, "value": 2.0, "pct": 50.0}, {"on": True, "value": 4.0, "pct": 50.0},
                          {"on": False, "value": 6.0, "pct": 0.0}]}
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
    def dismiss():
        wait(80)
        if page.is_visible("#dlg-hub-remind"): page.click("#hub-remind-ok"); wait(60)
    def openfn(fn):
        dismiss(); page.click(f'.hubx-item[data-fn="{fn}"]'); wait(150)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        names = page.eval_on_selector_all(".hubx-iname", "els=>els.map(e=>e.firstChild.textContent)")
        check("Break-Even is just after Stop Loss", names.index("Break-Even") == names.index("Stop Loss") + 1, str(names))
        check("Break-Even is not picked by default", page.evaluate("hubUi.picked.be") is False)
        page.click("#hub-confirm-btn"); wait(200)
        openfn("pair"); page.select_option("#hub-pair-select", "AAAUSDT"); wait(600); dismiss()
        check("plan before Break-Even: not picked", page.evaluate("hubUi.picked.be") is False)
        page.click("#hub-edit-btn"); wait(100)
        page.click('.hubx-item[data-fn="be"]'); wait(100)
        page.click("#hub-confirm-btn"); wait(200)
        openfn("be")
        check("2 tick boxes, not ticked, their numbers hidden", vis("be_after_tp") and vis("be_after_atr") and not page.is_checked("#hub_be_after_tp")
              and not page.is_checked("#hub_be_after_atr") and not vis("be_tp_n") and not vis("be_atr_value") and not vis("be_atr_length"))
        page.check("#hub_be_after_tp"); wait(120)
        check("After TP ticked: TP number shows", vis("be_tp_n") and not vis("be_atr_value"))
        page.check("#hub_be_after_atr"); wait(120)
        check("After ATR ticked: ATR (%) and ATR length show", vis("be_atr_value") and vis("be_atr_length"))
        for key in ("be_after_tp", "be_tp_n", "be_after_atr", "be_atr_value", "be_atr_length"):
            el = page.query_selector(f'#hubpane_be .info-i[data-help="{key}"]')
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        page.fill("#hub_be_tp_n", "1"); page.fill("#hub_be_atr_value", "100"); page.fill("#hub_be_atr_length", "14")
        page.press("#hub_be_atr_length", "Tab"); wait(100)
        openfn("sl"); page.select_option("#hub_sl_basis", "% of price movement"); dismiss()
        page.fill("#hub_sl_value", "3"); page.press("#hub_sl_value", "Tab"); dismiss()
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else page.inner_text("#hub-msg")
        check("review: Break-Even line", "Break-Even: Stop Loss to the entry price after TP 1 or after 100% of ATR (ATR length 14) (whichever comes first)" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        be = store.get_account(acc.id).pairs["AAAUSDT"].plan.get("be")
        check("saved", be == {"on": True, "after_tp": True, "tp_n": 1, "after_atr": True, "atr_value": 100, "atr_length": 14}, str(be))
        # TP 3 is not used: the server refuses with its words
        openfn("be"); page.fill("#hub_be_tp_n", "3"); page.press("#hub_be_tp_n", "Tab"); wait(100)
        page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(700)
        msg = page.inner_text("#hub-msg")
        check("After TP 3 with TP3 not used is refused", "TP3 is not used" in msg, msg[:200])
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
