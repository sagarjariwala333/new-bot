"""
browser_amend_check.py - REAL-browser check of the Save box "Change an open trade" (2026-11-05). OLD DOCSTRING:
browser_presets_check.py - OPTIONAL real-browser check of the PRESETS (2026-10-28): the Presets button on the Strategy Hub,
Load of "HMA Flip", "MACD Cross", "V3", "RSI MA Cross", "EMA Cross" and "MA 1 over MA 2" (rule choices only, NO NUMBERS), nothing left over from the other preset,
a preset cannot be changed, and a preset with arbitrary numbers keyed in is accepted by the real library route
(Playwright + Chromium). Backend = the REAL plan and library routes of app/api; no exchange calls.
Run:  python tests/browser_presets_check.py     (arbitrary numbers only, NOT the owner's settings)
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_amend")
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
store.add_pair(acc.id, tv.pair_config("AAAUSDT", plan=None, timeframe="4h"))
store.add_pair(acc.id, tv.pair_config("BBBUSDT", plan=None, timeframe="1h"))


STATE = {"status": "IN_POSITION", "puts": [], "answer": None}


class Body:
    def __init__(self, d): self.__dict__.update(d)


def route(method, path, payload):
    try:
        if path == "/api/auth/status": return 200, {"ok": True}
        if path == "/api/accounts": return 200, api.list_accounts()
        if path == "/api/okx/accounts": return 200, okx_api.list_okx_accounts()
        if path in ("/api/status", "/api/okx/status"): return 200, {"instances": [], "startup_failures": {}}
        if path.startswith(f"/api/status/{acc.id}/"):
            return (200, {"status": STATE["status"]}) if STATE["status"] else (404, {"detail": "instance not running"})
        if path == "/healthz": return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/default-settings": return 200, api.get_default_settings()
        if path == "/api/library" and method == "GET": return 200, asyncio.run(api.get_library())
        if path == "/api/library" and method == "POST": return 200, asyncio.run(api.save_to_library(Body(payload)))
        m = re.fullmatch(rf"/api/accounts/{acc.id}/pairs/([A-Z]+)(/plan|/start)?", path)
        if m:
            sym, tail = m.group(1), m.group(2) or ""
            if tail == "/plan" and method == "GET": return 200, asyncio.run(api.get_pair_plan(acc.id, sym))
            if tail == "/plan" and method == "PUT":
                STATE["puts"].append(payload)
                return 200, {"amend_result": STATE["answer"]}
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



PLAN = {"name": "x", "chart_type": "Candles"}


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def wait(ms=200): page.wait_for_timeout(ms)
    def box_shown(): return page.is_visible("#hub-amend-box")
    def checked(name): return page.evaluate("n => [...document.querySelectorAll('input[name=' + n + ']')].filter(x => x.checked).map(x => x.value)", name)
    def open_review(mode="hub-save-btn"):
        page.click("#" + mode); wait(500)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        # the form is not filled here: the plan is handed over, the Save box is what is tested
        page.evaluate("""() => { window.hubBuildPlan = () => ({name: 'x', chart_type: 'Candles'});
            window.hubSummaryLines = () => ['a line']; window.hubVisible = () => false; }""")
        page.evaluate("() => { const e = document.getElementById('hub_timeframe'); e.value = '4h'; e.dispatchEvent(new Event('input', {bubbles: true})); }")
        page.evaluate("() => { document.getElementById('hub-save-btn').disabled = false; }")

        # ---- a trade is open: the box is there, nothing is pre-selected, Confirm is off
        STATE["status"] = "IN_POSITION"
        open_review()
        check("trade open: the Save box is shown", box_shown())
        check("nothing is pre-selected", checked("amend-when") == [] and checked("amend-stops") == [])
        check("both choices are there (now / next)", page.locator('input[name="amend-when"]').count() == 2)
        check("the stops choice is hidden until 'now' is picked", not page.is_visible("#amend-stops"))
        check("Confirm is off until a choice is made", page.is_disabled("#hub-review-apply"))
        page.click('input[name="amend-when"][value="now"]'); wait(150)
        check("'now' shows the stops choice, both options, none pre-selected",
              page.is_visible("#amend-stops") and page.locator('input[name="amend-stops"]').count() == 2 and checked("amend-stops") == [])
        check("'now' without a stops choice: Confirm still off", page.is_disabled("#hub-review-apply"))
        page.click('input[name="amend-stops"][value="looser"]'); wait(150)
        check("'now' + stops chosen: Confirm on", not page.is_disabled("#hub-review-apply"))
        page.click('input[name="amend-when"][value="next"]'); wait(150)
        check("'next' needs no stops choice: Confirm on, stops hidden", not page.is_disabled("#hub-review-apply") and not page.is_visible("#amend-stops"))
        # send: now + tighter, server answers with what it did
        page.click('input[name="amend-when"][value="now"]'); wait(100)
        page.click('input[name="amend-stops"][value="tighter"]'); wait(100)
        STATE["puts"].clear()
        STATE["answer"] = {"status": "done", "applied": ["Stop Loss: now 98."], "kept": ["Max Loss per Trade: kept."], "waits": True}
        page.click("#hub-review-apply"); wait(700)
        sent = STATE["puts"][-1] if STATE["puts"] else {}
        check("the choice is sent with the plan", sent.get("amend") == {"when": "now", "stops": "tighter"} and "plan" in sent, str(sent))
        txt = page.inner_text("#hub-msg")
        check("the answer is shown: what changed, what was NOT changed, what waits",
              "Stop Loss: now 98." in txt and "NOT changed: Max Loss per Trade: kept." in txt and "next trade" in txt, txt)

        # ---- next trade
        open_review()
        check("box shown again and EMPTY (the last choice is not remembered)", box_shown() and checked("amend-when") == [] and page.is_disabled("#hub-review-apply"))
        page.click('input[name="amend-when"][value="next"]'); wait(100)
        STATE["puts"].clear(); STATE["answer"] = {"status": "next"}
        page.click("#hub-review-apply"); wait(700)
        sent = STATE["puts"][-1] if STATE["puts"] else {}
        check("'next' is sent without stops", sent.get("amend") == {"when": "next"}, str(sent))
        check("answer: the open trade keeps its rules", "keeps its rules" in page.inner_text("#hub-msg"), page.inner_text("#hub-msg"))

        # ---- failure answer is shown plainly
        open_review()
        page.click('input[name="amend-when"][value="now"]'); page.click('input[name="amend-stops"][value="tighter"]'); wait(100)
        STATE["answer"] = {"status": "error", "why": "exchange down"}
        page.click("#hub-review-apply"); wait(700)
        check("a failed change says the trade was NOT changed", "NOT changed (exchange down)" in page.inner_text("#hub-msg"), page.inner_text("#hub-msg"))

        # ---- Execute also shows it; no trade open: no box, old behaviour, nothing sent
        STATE["status"] = None
        open_review()
        check("no trade open (pair not running): no box, Confirm on", not box_shown() and not page.is_disabled("#hub-review-apply"))
        STATE["puts"].clear(); STATE["answer"] = None
        page.click("#hub-review-apply"); wait(700)
        sent = STATE["puts"][-1] if STATE["puts"] else {}
        check("no box: the plan is sent without any amend choice", "amend" not in sent and "plan" in sent, str(sent))
        STATE["status"] = "IDLE"
        open_review()
        check("pair flat (IDLE): no box", not box_shown())
        page.click("#dlg-hub-review button.ghost"); wait(150)
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    server.shutdown()
    print(f"{sum(res)}/{len(res)} passed")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
