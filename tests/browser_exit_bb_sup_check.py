"""
browser_exit_bb_sup_check.py - OPTIONAL real-browser check of Exit when, Bollinger Bands, Supertrend, the 'previous candle' option,
the Supertrend-line trailing stop and the Bollinger half (2026-11-04).
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_exit_bb_sup_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_exit_bb_sup")
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
PLAN1 = make_plan(ind_ma1={"entry": True}, ind_bb={"entry": True, "length": 20, "mult": 2.0, "n": 2},
                  ind_sup={"flip": True, "modes": ["Trend direction", "Fresh flip within N candles"], "atr_length": 10, "factor": 3.0, "n": 4},
                  exit_when={"on": True, "signal": "MA 2 only (the slower one) curves against the trade", "after": "Close and flip to the other side"},
                  trail={"on": True, "type": "Lowest low / highest high", "length": 10},
                  name="Exit bb sup")
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
    def fn(i): page.click(f'.hubx-item[data-fn="{i}"]'); wait(150)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        items = page.eval_on_selector_all(".hubx-item", "e => e.map(x => x.getAttribute('data-fn'))")
        check("Exit when is the second last item of the menu (Support / Resistance is the last)", len(items) > 1 and items[-2] == "exitwhen" and items[-1] == "sr", str(items))
        # ---- Exit when
        fn("exitwhen")
        check("saved plan: Exit when is picked, signal + after load", page.input_value("#hub_exit_signal").startswith("MA 2 only")
              and page.input_value("#hub_exit_after").startswith("Close and flip"))
        check("Signal and After boxes show when picked", vis("exit_signal") and vis("exit_after"))
        sig = page.eval_on_selector_all("#hub_exit_signal option", "o => o.map(x => x.value)")
        aft = page.eval_on_selector_all("#hub_exit_after option", "o => o.map(x => x.value)")
        check("Signal has the 2 choices", [s for s in sig if s] == ["Both MA 1 and MA 2 curve against the trade", "MA 2 only (the slower one) curves against the trade"], str(sig))
        check("After has the 2 choices", [s for s in aft if s] == ["Close and wait for a fresh signal", "Close and flip to the other side"], str(aft))
        page.select_option("#hub_exit_signal", "Both MA 1 and MA 2 curve against the trade")
        page.select_option("#hub_exit_after", "Close and wait for a fresh signal")
        save()
        ew = (plan_of("AAAUSDT") or {}).get("exit_when") or {}
        check("saved: on / Both / wait", ew.get("on") is True and ew.get("signal", "").startswith("Both") and ew.get("after", "").startswith("Close and wait"), str(ew) + page.inner_text("#hub-msg")[:200])
        # ---- Bollinger + Supertrend boxes
        fn("ind")
        check("Bollinger boxes show (ticked): length, multiplier, N", vis("bb_length") and vis("bb_mult") and vis("bb_n"))
        check("Bollinger numbers load 20 / 2 / 2", (page.input_value("#hub_bb_length"), page.input_value("#hub_bb_mult"), page.input_value("#hub_bb_n")) == ("20", "2", "2"))
        check("Supertrend boxes show: ATR length, factor", vis("sup_atr_length") and vis("sup_factor"))
        check("Supertrend N shows when Fresh flip is ticked", vis("sup_n") and page.input_value("#hub_sup_n") == "4")
        flipbox = page.query_selector('#hub_sup_modes input[type=checkbox]:nth-of-type(1)')
        boxes = page.query_selector_all("#hub_sup_modes input[type=checkbox]")
        check("Supertrend has 2 condition ticks, both on", len(boxes) == 2 and all(x.is_checked() for x in boxes))
        boxes[1].click(); wait(150); dismiss()
        check("untick Fresh flip: N hides", not vis("sup_n"))
        boxes[1].click(); wait(150); dismiss()
        check("tick it again: N shows", vis("sup_n"))
        check("Candle 'previous candle' option exists", page.query_selector("#hub_candle_prev_opposite") is not None)
        check("Bollinger has a CTREND tick, Supertrend has none", page.query_selector("#hub_bb_ctrend") is not None and page.query_selector("#hub_sup_ctrend") is None)
        page.fill("#hub_bb_n", "3"); page.fill("#hub_sup_factor", "2.5")
        save()
        pl = plan_of("AAAUSDT") or {}
        check("saved: bb n 3, sup factor 2.5", pl.get("ind", {}).get("bb", {}).get("n") == 3 and pl.get("ind", {}).get("sup", {}).get("factor") == 2.5, page.inner_text("#hub-msg")[:200])
        # ---- Bollinger half (counter trend)
        page.check("#hub_bb_ctrend"); wait(150); dismiss()
        fn("size")
        check("Counter trend 'decided by' is the Bollinger half", "Bollinger" in (page.eval_on_selector("#hub_counter_by", "e => e.options[e.selectedIndex].text") or ""))
        # ---- Trailing: Supertrend line
        fn("trail")
        opts = page.eval_on_selector_all("#hub_trail_type option", "o => o.map(x => x.value)")
        check("Trailing type has 'Supertrend line'", "Supertrend line" in opts)
        page.select_option("#hub_trail_type", "Supertrend line"); wait(200); dismiss()
        check("Supertrend line shows no extra boxes", not vis("trail_c_value") and not vis("trail_basis") and not vis("trail_len") and not vis("trail_ma"))
        fn("ind")
        check("Supertrend numbers stay forced for the trail (boxes visible)", vis("sup_atr_length") and vis("sup_factor"))
        # ---- review lines
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review shows Exit when", "Exit when:" in rv, rv[:900])
        check("review shows the Supertrend trail", "Supertrend line" in rv, rv[:900])
        check("review shows the Bollinger half", "Bollinger half" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"): page.click("#dlg-hub-review button.ghost"); wait(100)
        # ---- info icons
        for key in ("fn.exitwhen", "exit_signal", "exit_after", "bb_length", "bb_mult", "bb_n", "sup_modes", "sup_atr_length", "sup_factor", "candle_prev_opposite", "ind.bb", "ind.sup"):
            ok = bool(page.evaluate("k => (typeof HELP !== 'undefined') && !!HELP[k]", key))
            check(f"help text exists: {key}", ok)
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
