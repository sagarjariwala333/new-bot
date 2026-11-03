"""
browser_stop_loss_check.py - OPTIONAL real-browser check (2026-10-26): Stop Loss tab - a must (always picked), the 6
"Measured on" choices with their own boxes, the optional Time stop, a plan saved without a Stop Loss.
Backend = the REAL plan routes; no exchange calls.  Run:  python tests/browser_stop_loss_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_stop_loss")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_ma1={"flip": True}, name="Old plan, no SL")
PLAN1.pop("sl", None)                                                    # saved before Stop Loss was a must
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
    def basis(v): page.select_option("#hub_sl_basis", v); dismiss()
    ATR, PRICE, USD, STRUCT, CHAND, MA = ("% of ATR", "% of price movement", "Fixed $ amount", "Structure (swing low / high)",
                                          "Chandelier", "Moving average")
    BOXES = ["sl_value", "sl_atr", "sl_usd", "sl_length", "sl_ma"]
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        check("Stop Loss is picked from the start", page.evaluate("hubUi.picked.sl") is True)
        page.click('.hubx-item[data-fn="sl"]'); wait(100)
        check("Stop Loss cannot be unpicked (a must)", page.evaluate("hubUi.picked.sl") is True)
        check("menu text: no SL = the trade still opens + email", "the trade still opens" in page.inner_text('.hubx-item[data-fn="sl"]'))
        page.click("#hub-confirm-btn"); wait(200)
        openfn("sl")
        opts = page.eval_on_selector_all("#hub_sl_basis option", "os => os.map(o => o.value).filter(Boolean)")
        check("6 Measured on choices", opts == [ATR, PRICE, USD, STRUCT, CHAND, MA], str(opts))
        check("no use / no-use box (it is a must)", not vis("sl_on"))
        want = {ATR: ["sl_value", "sl_atr"], PRICE: ["sl_value"], USD: ["sl_usd"], STRUCT: ["sl_length"],
                CHAND: ["sl_value", "sl_atr", "sl_length"], MA: ["sl_ma"]}
        for k, w in want.items():
            basis(k)
            shown = [x for x in BOXES if vis(x)]
            check(f"{k}: shows {w}", shown == w, str(shown))
        openfn("ind")
        check("Moving average not set yet: MA 1 / MA 2 numbers hidden", not page.is_visible("#hubcard_ma1") and not page.is_visible("#hubcard_ma2"))
        openfn("sl"); page.select_option("#hub_sl_ma", "MA 2"); dismiss()
        openfn("ind")
        check("Stop Loss at MA 2: MA 2 numbers show in Indicators", page.is_visible("#hubcard_ma2"))
        openfn("sl")
        check("Time stop box shown, not ticked, N hidden", vis("sl_time_on") and not page.is_checked("#hub_sl_time_on") and not vis("sl_time_candles"))
        page.check("#hub_sl_time_on"); dismiss()
        check("Time stop ticked: N candles shows", vis("sl_time_candles"))
        for key in ("sl_basis", "sl_value", "sl_usd", "sl_length", "sl_ma", "sl_time_on", "sl_time_candles", "sl_atr"):
            el = page.query_selector(f'#hubpane_sl .info-i[data-help="{key}"]')
            if el is None or not el.is_visible():
                for k in (CHAND, USD, MA):
                    basis(k); el = page.query_selector(f'#hubpane_sl .info-i[data-help="{key}"]')
                    if el is not None and el.is_visible(): break
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        # ---- a plan saved without a Stop Loss
        openfn("pair"); page.select_option("#hub-pair-select", "AAAUSDT"); wait(600)
        dismiss()
        check("old plan: Stop Loss is picked", page.evaluate("hubUi.picked.sl") is True)
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else page.inner_text("#hub-msg")
        check("old plan: can save without SL (review says NOT SET)", "Stop Loss: NOT SET (trades still open, you get an email: No SL was set for this trade. Set it manually.)" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        check("saved without SL", store.get_account(acc.id).pairs["AAAUSDT"].plan.get("sl") == {"on": False},
              str(store.get_account(acc.id).pairs["AAAUSDT"].plan.get("sl")))
        openfn("sl")
        basis(STRUCT); page.fill("#hub_sl_length", "10"); page.press("#hub_sl_length", "Tab"); dismiss()
        page.check("#hub_sl_time_on"); dismiss(); page.fill("#hub_sl_time_candles", "5"); page.press("#hub_sl_time_candles", "Tab"); dismiss()
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else page.inner_text("#hub-msg")
        check("review: Stop Loss line", "Stop Loss: Structure (swing low / high) · N candles: 10 · Time stop: 5 candles, not in profit = close" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        sl = store.get_account(acc.id).pairs["AAAUSDT"].plan.get("sl")
        check("saved: Structure 10, time stop 5", sl == {"on": True, "basis": STRUCT, "length": 10, "time_on": True, "time_candles": 5}, str(sl))
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
