"""
browser_analysis_check.py - OPTIONAL real-browser check of the Analysis tab on the pair's Strategy Hub plan
(2026-10-24): Walk-Forward and Monte Carlo. Backend = the REAL analysis routes of app/api with synthetic past
candles (no exchange calls).  Run:  python tests/browser_analysis_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_analysis")
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
PLAN1 = make_plan(ind_ma1={"entry": True, "flip": True, "type": "EMA", "length": 20, "mode": "Price above / below MA"},
                  sl={"on": True, "basis": "% of price movement", "value": 2.0}, name="Analysis test")
from tests.test_strategy_hub import make_df  # noqa: E402
_DF = make_df(1200, seed=3)


async def _fake_history(account_id, symbol, timeframe, limit):
    return _DF.tail(limit).reset_index(drop=True)


api._fetch_history = _fake_history
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
        m = re.fullmatch(rf"/api/analysis/{acc.id}/([A-Z]+)/(walk-forward|monte-carlo)", path)
        if m and method == "POST":
            fn = api.walk_forward if m.group(2) == "walk-forward" else api.monte_carlo
            body_cls = api.WalkForwardRequest if m.group(2) == "walk-forward" else api.MonteCarloRequest
            return 200, asyncio.run(fn(acc.id, m.group(1), body_cls(**payload)))
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
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-analysis"); wait(300)
        check("subtitle says it replays the Strategy Hub plan", "Strategy Hub plan" in page.inner_text("#tabpage-analysis .sub"))
        opts = page.eval_on_selector_all("#analysis-pair-select option", "o => o.map(x => x.value)")
        pick = next((o for o in opts if o.endswith("::AAAUSDT")), None)
        check("the pair with a plan is in the list", pick is not None, str(opts))
        page.select_option("#analysis-pair-select", pick); wait(200)
        for k, v in (("wf-folds", "3"), ("wf-limit", "1000"), ("wf-equity", "1000"), ("wf-commission", "0.04"), ("wf-slippage", "0.01")):
            page.fill("#" + k, v)
        page.click("#wf-run"); wait(4000)
        msg = page.inner_text("#wf-msg")
        rows = page.eval_on_selector_all("#wf-table tbody tr", "r => r.map(x => x.innerText)")
        check("Walk-Forward runs on the Hub plan", "Strategy Hub plan was replayed" in msg, msg[:300])
        check("3 folds + the whole period row", len(rows) == 4 and rows[-1].startswith("Whole period"), str(rows)[:400])
        check("trades-off column shown", "not counted (trades off)" in page.inner_text("#wf-table thead").lower())
        page.click("#tab-mc"); wait(150)
        for k, v in (("mc-sims", "200"), ("mc-limit", "1000"), ("mc-equity", "1000"), ("mc-commission", "0.04"), ("mc-slippage", "0.01")):
            page.fill("#" + k, v)
        page.click("#mc-run"); wait(4000)
        mt = page.inner_text("#mc-msg")
        check("Monte Carlo runs", mt.startswith("Ran 200 simulations"), mt[:300])
        check("Monte Carlo results shown", "Median final equity" in page.inner_text("#mc-results"))
        pick2 = next((o for o in opts if o.endswith("::BBBUSDT")), None)
        page.select_option("#analysis-pair-select", pick2); wait(200)
        page.click("#tab-wf"); wait(100)
        for k, v in (("wf-folds", "3"), ("wf-limit", "1000"), ("wf-equity", "1000"), ("wf-commission", "0.04"), ("wf-slippage", "0.01")):
            page.fill("#" + k, v)
        page.click("#wf-run"); wait(1500)
        m2 = page.inner_text("#wf-msg")
        check("a pair with no Hub plan is refused with a clear message", "no Strategy Hub plan" in m2, m2[:300])
        el = page.query_selector('.info-i[data-help="an.pair"]')
        el.click(); wait(150)
        check("the Pair i says it tests the Hub plan", "Strategy Hub plan" in page.inner_text("#dlg-help") and "Base V3" not in page.inner_text("#dlg-help"))
        page.click("#dlg-help button.ghost"); wait(100)
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
