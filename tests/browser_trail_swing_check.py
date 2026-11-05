"""
browser_trail_swing_check.py - OPTIONAL real-browser check of the 4th Trailing Stop type "Signal candle high / low +/- ATR"
(2026-10-28): Distance (% of ATR) and ATR length, the review line and the i texts.
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_trail_candle_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_trail_swing")
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
PLAN1 = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": "Structure (swing low / high)", "length": 5},
                  trail={"on": True, "type": "Signal candle high / low +/- ATR", "value": 100.0, "atr_length": 14},
                  name="Candle trail")
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
    def ticked(fid): return [page.is_checked(f"#hub_{fid}__{i}") for i in (0, 1)]
    def dismiss():
        wait(80)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); wait(60)
    def tick(fid, i):
        page.check(f"#hub_{fid}__{i}"); wait(150); dismiss()
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

        # ---------------- Trailing Stop: when the Signal candle type may take charge
        page.click('.hubx-item[data-fn="trail"]'); wait(150)
        check("old plan loads with 'Tighter than the Stop Loss' ticked (how it always worked)", ticked("trail_c_start") == [True, False], str(ticked("trail_c_start")))
        check("the in-profit number is hidden until that choice is ticked", vis("trail_c_start") and not vis("trail_c_profit"))
        tick("trail_c_start", 1)
        check("tick one: ticking 'Only once in profit' unticks the other", ticked("trail_c_start") == [False, True], str(ticked("trail_c_start")))
        check("the in-profit number shows and starts empty (no number preset)", vis("trail_c_profit") and page.input_value("#hub_trail_c_profit") == "")
        save()
        msg = page.inner_text("#hub-msg")
        check("an empty in-profit number is refused, the saved plan is unchanged", msg.startswith("Not saved") and "candle_start" not in (plan_of("AAAUSDT").get("trail") or {}), msg[:200])
        page.fill("#hub_trail_c_profit", "75")
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review line shows the in-profit rule", "only after a candle closes 75% of ATR beyond the entry" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"): page.click("#hub-review-apply"); wait(800)
        tr = plan_of("AAAUSDT").get("trail") or {}
        check("saved: only once in profit and 75", (tr.get("candle_start"), tr.get("candle_profit")) == ("Only once in profit", 75), str(tr))
        page.click('.hubx-item[data-fn="sl"]'); wait(100); page.click('.hubx-item[data-fn="trail"]'); wait(150)
        check("saved choice and number come back after reopening", ticked("trail_c_start") == [False, True] and page.input_value("#hub_trail_c_profit") == "75")
        tick("trail_c_start", 0)
        check("back to 'Tighter': the number hides", not vis("trail_c_profit"))
        save()
        tr = plan_of("AAAUSDT").get("trail") or {}
        check("saved with the old choice: no choice key and no profit number in the plan (same as an old plan)", "candle_start" not in tr and "candle_profit" not in tr, str(tr))
        page.select_option("#hub_trail_type", "Step"); wait(150); dismiss()
        check("another trailing type: the new choice hides", not vis("trail_c_start") and not vis("trail_c_profit"))
        page.select_option("#hub_trail_type", "Signal candle high / low +/- ATR"); wait(150); dismiss()
        check("back to the candle type: the choice shows again", vis("trail_c_start"))

        # ---------------- Stop Loss: how Structure is found
        page.click('.hubx-item[data-fn="sl"]'); wait(150)
        check("old plan: 'Lowest low / highest high' ticked, its N candles shown, swing N hidden",
              ticked("sl_struct_mode") == [True, False] and vis("sl_length") and not vis("sl_struct_n"), str(ticked("sl_struct_mode")))
        tick("sl_struct_mode", 1)
        check("real swing point: its N shows and starts empty, the old N hides", vis("sl_struct_n") and page.input_value("#hub_sl_struct_n") == "" and not vis("sl_length"))
        save()
        msg = page.inner_text("#hub-msg")
        check("an empty swing N is refused", msg.startswith("Not saved") and "struct_mode" not in (plan_of("AAAUSDT").get("sl") or {}), msg[:200])
        page.fill("#hub_sl_struct_n", "3")
        page.click("#hub-save-btn"); wait(400)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); wait(100); page.click("#hub-save-btn"); wait(400)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review line shows the real swing point", "Real swing point, 3 candles on each side" in rv, rv[:900] + "|" + page.inner_text("#hub-msg")[:200])
        if page.is_visible("#dlg-hub-review"): page.click("#hub-review-apply"); wait(800)
        sl = plan_of("AAAUSDT").get("sl") or {}
        check("saved: real swing point and 3", (sl.get("struct_mode"), sl.get("struct_n")) == ("Real swing point (N candles each side)", 3), str(sl))
        page.select_option("#hub_sl_basis", "Chandelier"); wait(150); dismiss()
        check("Chandelier: its N candles box shows, the structure choice hides", vis("sl_length") and not vis("sl_struct_mode") and not vis("sl_struct_n"))
        page.select_option("#hub_sl_basis", "Structure (swing low / high)"); wait(150); dismiss()
        tick("sl_struct_mode", 0)
        page.fill("#hub_sl_length", "5")
        save()
        sl = plan_of("AAAUSDT").get("sl") or {}
        check("saved with the old choice: no choice key (same as an old plan), N candles kept", "struct_mode" not in sl and "struct_n" not in sl and sl.get("length") == 5, str(sl))

        for key in ("trail_c_start", "trail_c_profit", "sl_struct_mode", "sl_struct_n"):
            el = page.query_selector(f'.info-i[data-help="{key}"]')
            if el is None or not el.is_visible():
                # the box is hidden right now: make it visible, then look for its icon
                if key.startswith("trail"):
                    page.click('.hubx-item[data-fn="trail"]'); wait(100)
                    if key == "trail_c_profit": tick("trail_c_start", 1)
                else:
                    page.click('.hubx-item[data-fn="sl"]'); wait(100)
                    if key == "sl_struct_n": tick("sl_struct_mode", 1)
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
