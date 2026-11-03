"""
browser_hub_stops_check.py - OPTIONAL real-browser check of the 2026-10-23 Hub boxes: Stop Loss, Trailing Stop
types, Take profit basis, the Max loss / Stop Loss reminder pop-ups and Allow Mid-Trend Re-entry (Playwright + Chromium).
Backend = the REAL plan routes of app/api; no exchange calls. Run:  python tests/browser_hub_stops_check.py
"""
import asyncio, json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_stops")
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
from tests import _test_values as tv

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
store = store_module.Store(); okx_store = okx_store_module.OKXStore()
api.store = store; okx_api.okx_store = okx_store


class _Mgr:
    def get(self, *a): return None
    async def restart(self, *a): pass


api.manager = _Mgr()
acc = store.create_account("Test", "k", "s", True)
store.add_pair(acc.id, tv.pair_config("TESTUSDT"))
okx_acc = okx_store.create_account("Demo", "k", "s", "p", demo=True)


STARTED = []
START_FAILS = []


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
        base = f"/api/accounts/{acc.id}/pairs/TESTUSDT"
        if path == base + "/plan" and method == "GET":
            return 200, asyncio.run(api.get_pair_plan(acc.id, "TESTUSDT"))
        if path == base + "/plan" and method == "PUT":
            return 200, asyncio.run(api.set_pair_plan(acc.id, "TESTUSDT", Body(payload)))
        if path == base and method == "PUT":
            store.update_pair(acc.id, "TESTUSDT", payload); return 200, {}
        if path == base + "/start" and method == "POST":
            if START_FAILS:
                START_FAILS.pop(); return 400, {"detail": "Refusing to start - live trading is OFF"}
            STARTED.append(path); return 200, {"status": "IDLE"}
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



def only(page, fid, value):
    """Condition ticks (2026-10-25): tick exactly this one condition (real clicks)."""
    for x in page.query_selector_all(f"#hub_{fid} input[type=checkbox]"):
        want = x.get_attribute("value") == value
        if x.is_checked() != want:
            x.click()
    page.wait_for_timeout(120)


def ticked(page, fid):
    return page.eval_on_selector_all(f"#hub_{fid} input[type=checkbox]", "xs => xs.filter(x => x.checked).map(x => x.value)")



def pick(page, fid, value):
    """"Tick one" boxes (2026-10-25: Size method): tick the box with this value ("" = untick all)."""
    for x in page.query_selector_all(f"#hub_{fid} input[type=checkbox]"):
        want = x.get_attribute("value") == value
        if x.is_checked() != want:
            x.click()
    page.wait_for_timeout(120)


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def remind_text():
        page.wait_for_timeout(120)
        return page.inner_text("#dlg-hub-remind") if page.is_visible("#dlg-hub-remind") else ""
    def dismiss():
        page.wait_for_timeout(80)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); page.wait_for_timeout(60)
    def openfn(fn):
        dismiss(); page.click(f'.hubx-item[data-fn="{fn}"]'); page.wait_for_timeout(120)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); page.wait_for_timeout(600)
        page.click("#maintab-hub"); page.wait_for_timeout(300)
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(300)
        page.click('.hubx-item[data-fn="hold"]'); page.wait_for_timeout(60)   # Hold rule starts picked (2026-10-25): unpick here
        for fn in ("maxloss", "sl", "trail", "tp"):
            page.click(f'.hubx-item[data-fn="{fn}"]'); page.wait_for_timeout(60)
        check("Max loss, Stop Loss, Trailing Stop, Take profit picked", page.inner_text("#hub-progress").startswith("11 picked"), page.inner_text("#hub-progress"))
        page.click("#hub-confirm-btn"); page.wait_for_timeout(200)
        page.select_option("#hub-pair-select", index=1); page.wait_for_timeout(500)
        # ---- Entry: Allow Mid-Trend Re-entry, not ticked by default
        openfn("wait")
        check("No Mid-Trend Re-entry box is on the Entry wait page", page.is_visible("#hub_reentry"))
        check("No Mid-Trend Re-entry is ticked by default (re-entry not allowed)", page.is_checked("#hub_reentry"))
        check("Candles Wait starts at 0, Fresh Reversal on First Trade ticked", page.input_value("#hub_entry_wait") == "0" and page.is_checked("#hub_wfr"))
        # ---- Stop Loss
        openfn("sl")
        check("Stop Loss: Measured on shown (Value shows after the choice)", page.is_visible("#hub_sl_basis") and not page.is_visible("#hub_sl_value"))
        check("Stop Loss: ATR length hidden until % of ATR", not page.is_visible("#hub_sl_atr"))
        page.select_option("#hub_sl_basis", "% of ATR")
        t = remind_text()
        check("changing Stop Loss pops up the Max loss settings", "MAX LOSS PER TRADE SETTINGS" in t.upper(), t[:200])
        dismiss()
        check("ATR length shown for % of ATR", page.is_visible("#hub_sl_atr"))
        page.select_option("#hub_sl_basis", "% of price movement"); dismiss()
        check("ATR length hidden for % of price movement", not page.is_visible("#hub_sl_atr"))
        page.fill("#hub_sl_value", "3"); page.press("#hub_sl_value", "Tab")
        t = remind_text(); dismiss()
        check("the reminder accepts the input (nothing is cleared)", page.input_value("#hub_sl_value") == "3" and "Max Loss per Trade" in t)
        # ---- Max loss: its change shows the Stop Loss settings
        openfn("maxloss")
        page.select_option("#hub_stop_basis", "% of available balance")
        t = remind_text()
        check("changing Max loss pops up the Stop Loss settings", "STOP LOSS SETTINGS" in t.upper() and "% of price movement" in t and "3" in t, t[:200])
        dismiss()
        page.fill("#hub_stop_pct", "6"); page.press("#hub_stop_pct", "Tab"); dismiss()
        # ---- Trailing Stop types
        openfn("trail")
        check("Trailing Stop: only the type shows first", page.is_visible("#hub_trail_type") and not page.is_visible("#hub_trail_gap") and not page.is_visible("#hub_trail_len"))
        page.select_option("#hub_trail_type", "Step")
        check("Step: Measured on, Start and Gap shown", page.is_visible("#hub_trail_basis") and page.is_visible("#hub_trail_start") and page.is_visible("#hub_trail_gap"))
        check("Step: start value hidden until Own value", not page.is_visible("#hub_trail_start_value"))
        page.select_option("#hub_trail_start", "Own value")
        check("Own value shows the start value box", page.is_visible("#hub_trail_start_value"))
        page.select_option("#hub_trail_start", "Same as Stop Loss")
        check("Same as Stop Loss hides it again", not page.is_visible("#hub_trail_start_value"))
        page.select_option("#hub_trail_type", "Lowest low / highest high")
        check("Lowest low / highest high: look-back only", page.is_visible("#hub_trail_len") and not page.is_visible("#hub_trail_gap"))
        page.select_option("#hub_trail_type", "Moving average")
        check("Moving average: which MA shown", page.is_visible("#hub_trail_ma"))
        page.select_option("#hub_trail_ma", "MA 2")
        openfn("ind")
        check("a Moving average Trailing Stop shows that MA's numbers in Indicators", page.is_visible("#hubcard_ma2") and not page.is_visible("#hubcard_ma1"))
        # ---- fill a Step plan
        page.check("#hub_ma1_entry")      # the flip is Same as entry (2026-10-25 default Yes)
        page.select_option("#hub_ma1_type", "EMA"); page.fill("#hub_ma1_length", "20"); only(page, "ma1_modes", "Price above / below MA")
        openfn("trail")
        page.select_option("#hub_trail_type", "Step"); page.select_option("#hub_trail_basis", "% of price movement")
        page.select_option("#hub_trail_start", "Same as Stop Loss"); page.fill("#hub_trail_gap", "0.5")
        openfn("tp")
        check("Take profit: ATR length hidden until % of ATR", not page.is_visible("#hub_tp_atr"))
        page.select_option("#hub_tp_basis", "% of price movement")
        page.check("#hub_tp1_on"); page.fill("#hub_tp1_value", "3"); page.fill("#hub_tp1_pct", "80")
        openfn("pair"); page.select_option("#hub_timeframe", "1h")
        openfn("chart"); page.select_option("#hub_chart_type", "Candles")
        openfn("combine"); page.select_option("#hub_combine_entry", "ALL")   # FLIP: Same as entry (no own combination)
        openfn("flipact")
        openfn("wait"); page.fill("#hub_entry_wait", "0"); page.press("#hub_entry_wait", "Tab")
        openfn("size"); pick(page, "size_method", "Allocation % of balance"); page.fill("#hub_alloc_pct", "10"); page.fill("#hub_leverage", "5")
        page.click("#hub-save-btn"); page.wait_for_timeout(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else page.inner_text("#hub-msg")
        check("review lists Stop Loss, Trailing Stop, take profit and re-entry", "Stop Loss: % of price movement · Value: 3 % · Time stop: OFF" in rv and "Trailing Stop: Step, start same as Stop Loss, gap 0.5 % of price movement" in rv
              and "TP1 at 3% (closes 80%)" in rv and "No Mid-Trend Re-entry: ON" in rv and "Max Loss per Trade (last line of defense)" in rv, rv[:900])
        page.click("#hub-review-apply"); page.wait_for_timeout(800)
        pl = store.get_account(acc.id).pairs["TESTUSDT"].plan or {}
        check("Stop Loss saved", pl.get("sl") == {"on": True, "basis": "% of price movement", "value": 3, "time_on": False}, str(pl.get("sl")))
        check("Step Trailing Stop saved", pl.get("trail") == {"on": True, "type": "Step", "basis": "% of price movement", "start": "Same as Stop Loss", "gap": 0.5}, str(pl.get("trail")))
        check("take profit saved in % of price movement", pl.get("tp", {}).get("basis") == "% of price movement" and pl["tp"]["levels"][0].get("value") == 3, str(pl.get("tp")))
        check("Allow Mid-Trend Re-entry saved OFF", pl.get("allow_reentry") is False)
        # ---- Stop Loss is a must (2026-10-26): it cannot be unpicked
        page.click("#hub-edit-btn"); page.wait_for_timeout(100)
        page.click('.hubx-item[data-fn="sl"]'); page.wait_for_timeout(60)
        check("Stop Loss cannot be unpicked (a must)", page.evaluate("hubUi.picked.sl") is True)
        page.click("#hub-confirm-btn"); page.wait_for_timeout(200)
        # ---- Hold rule is a pick in the menu (2026-10-24, one tab per function)
        page.click("#hub-edit-btn"); page.wait_for_timeout(100)
        page.click('.hubx-item[data-fn="hold"]'); page.wait_for_timeout(60)
        page.click("#hub-confirm-btn"); page.wait_for_timeout(200)
        openfn("hold")
        check("Hold rule picked: only its ADX level box shows (no extra tick box)", page.is_visible("#hub_hold_level") and not page.is_visible("#hub_hold_on"))
        openfn("ind")
        check("Hold rule picked: the ADX / DI numbers show in Indicators", page.is_visible("#hubcard_adx"))
        page.select_option("#hub_adx_di_length", "") if False else None
        page.fill("#hub_adx_di_length", "14"); page.fill("#hub_adx_smoothing", "14"); page.fill("#hub_adx_level", "20")
        openfn("hold"); page.fill("#hub_hold_level", "25")
        for fn in ("pair", "chart", "combine", "flipact", "wait", "size", "maxloss", "sl", "trail", "tp"):
            openfn(fn)
        page.click("#hub-save-btn"); page.wait_for_timeout(300)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); page.wait_for_timeout(800)
        pl = store.get_account(acc.id).pairs["TESTUSDT"].plan or {}
        check("Hold rule saved ON with its level", pl.get("hold") == {"on": True, "adx_level": 25}, str(pl.get("hold")) + " " + page.inner_text("#hub-msg")[:200])
        # ---- phone
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
