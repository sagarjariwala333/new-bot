"""
browser_tp_from_check.py - OPTIONAL real-browser check of Take profit "Measured from" (2026-10-27):
Entry price / Signal candle close, its i text, the review line, and plans saved before it.
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_tp_from_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_tp_from")
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
PLAN1 = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": "% of ATR", "value": 200.0, "atr_length": 14},
                  tp={"on": True, "basis": "% of ATR", "atr_length": 14,
                      "levels": [{"on": True, "value": 100.0, "pct": 50.0}, {"on": True, "value": 200.0, "pct": 50.0},
                                 {"on": False, "value": 300.0, "pct": 1.0}]}, name="TP from test")
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
    def vis(i): return page.is_visible("#hub_" + i)
    def plan_of(sym): return store.get_account(acc.id).pairs[sym].plan
    def dismiss():
        wait(80)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); wait(60)
    def save():
        dismiss(); page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        page.click('.hubx-item[data-fn="tp"]'); wait(150)
        check("Measured from shows, with both choices",
              vis("tp_from") and page.eval_on_selector_all("#hub_tp_from option", "o => o.map(x => x.value)")[1:] == ["Entry price", "Signal candle close"])
        check("a plan saved before the choice loads as Entry price", page.input_value("#hub_tp_from") == "Entry price")
        check("Measured on and the 3 targets are still there", vis("tp_basis") and vis("tp_atr") and vis("tp1_value") and vis("tp2_value"))
        el = page.query_selector('.info-i[data-help="tp_from"]')
        ok = el is not None and el.is_visible()
        if ok:
            el.click(); wait(150); ok = page.is_visible("#dlg-help"); txt = page.inner_text("#dlg-help") if ok else ""
            page.click("#dlg-help button.ghost"); wait(100)
            check("info icon opens the explanation (mentions Signal candle close)", "Signal candle close" in txt, txt[:200])
        else:
            check("info icon is there", False)
        page.select_option("#hub_tp_from", "Signal candle close"); wait(100)
        save()
        tp = (plan_of("AAAUSDT") or {}).get("tp") or {}
        check("saved: from = Signal candle close, the rest unchanged",
              tp.get("from") == "Signal candle close" and tp.get("basis") == "% of ATR" and tp.get("levels", [{}])[0].get("value") == 100,
              str(tp) + " " + page.inner_text("#hub-msg")[:200])
        page.click('.hubx-item[data-fn="pair"]'); wait(100)
        page.click('.hubx-item[data-fn="tp"]'); wait(100)
        check("reloaded form shows Signal candle close", page.input_value("#hub_tp_from") == "Signal candle close")
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review line says from Signal candle close", "from Signal candle close" in rv, rv[:600])
        if page.is_visible("#dlg-hub-review"): page.click("#dlg-hub-review button.ghost"); wait(100)
        page.select_option("#hub_tp_from", "Entry price"); save()
        tp = (plan_of("AAAUSDT") or {}).get("tp") or {}
        check("saved back: from = Entry price", tp.get("from") == "Entry price", str(tp))
        page.select_option("#hub_tp_from", ""); save()
        tp = (plan_of("AAAUSDT") or {}).get("tp") or {}
        check("left empty: saved without the key (= Entry price) and still valid", "from" not in tp and tp.get("on") is True, str(tp) + " " + page.inner_text("#hub-msg")[:200])
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
