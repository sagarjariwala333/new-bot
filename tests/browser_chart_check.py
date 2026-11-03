"""
browser_chart_check.py - OPTIONAL real-browser check of the Chart tab (real candles on top, shadow tracker below).
Needs Playwright + Chromium. The candles come from a fake exchange client; the chart code and the API route are real.
Backend = the REAL plan routes of app/api; no exchange calls. Run:  python tests/browser_chart_check.py
"""
import asyncio, json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_chart")
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



# ---- chart data: real route, fake candles, a shadow book and an open real trade in the hub memory
import math as _math
from app import chart_api, hub_state, shadow_book as _B, ledger as _ledger
import unittest.mock as _mock


class _Klines:
    async def get_klines(self, symbol, interval, limit=500):
        rows = []
        p = 100.0
        for k in range(300):
            o = p
            p = p * (1 + 0.004 * _math.sin(k / 7.0)) + 0.05
            rows.append([1_700_000_000_000 + k * 3_600_000, str(o), str(max(o, p) + 0.4), str(min(o, p) - 0.4), str(p),
                         "1", 0, 0, 0, 0, 0, 0])
        return rows[-limit:]

    async def close(self):
        pass


class _PC:
    timeframe = "1h"
    plan = {"shadow": {"on": True}}


_hs = hub_state.HubState(platform="binance", account_id=acc.id, symbol="TESTUSDT")
_book = _B.new_book(1000.0)
_book["curve"] = [[1_700_000_000_000 + k * 3_600_000 + 10, 1000.0 + 3 * _math.sin(k / 9.0) + k * 0.05] for k in range(100, 300)]
_book["trades"] = [{"direction": "LONG", "entry_time": 1_700_000_000_000 + 250 * 3_600_000, "signal_close": 101.0,
                    "exit_time": 1_700_000_000_000 + 260 * 3_600_000 + 5, "exit_price": 102.0, "exit_reason": "FLIP",
                    "result_pct": 0.5}]
_hs.shadow_book = _book
_hs.ladder = {"direction": "LONG", "entry": 100.0, "ml": 95.0, "sl": 99.0, "ts": None, "ex": {"name": "sl", "kind": "limit"}}
hub_state.save(_hs)
_rec = _ledger.TradeRecord(account_id=acc.id, symbol="TESTUSDT", direction="SHORT", qty=1.0, entry_price=103.0,
                           exit_price=102.0, pnl=1.0, reason="SL", opened_at=(1_700_000_000_000 + 270 * 3_600_000) / 1000.0,
                           closed_at=(1_700_000_000_000 + 280 * 3_600_000) / 1000.0)
_mock.patch.object(chart_api, "_pair_and_client", return_value=(_PC, _Klines(), False)).start()
_mock.patch.object(chart_api.ledger, "list_trades", return_value=[_rec]).start()
CHART_CALLS = []
_orig_route = route


def route(method, path, payload):  # noqa: F811
    if path.startswith("/api/chart/"):
        CHART_CALLS.append(path)
        parts = path.split("/")
        return 200, asyncio.run(chart_api.chart_data(parts[3], parts[4], parts[5], 500))
    return _orig_route(method, path, payload)


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


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    INK = """(id) => { const c = document.getElementById(id); const g = c.getContext('2d');
        const d = g.getImageData(0, 0, c.width, c.height).data; let n = 0, orange = 0;
        for (let i = 0; i < d.length; i += 4) { if (d[i+3] > 0) n++; if (d[i] > 200 && d[i+1] > 140 && d[i+1] < 190 && d[i+2] < 110) orange++; }
        return [n, orange]; }"""
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1400, "height": 1000})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); page.wait_for_timeout(600)
        check("Chart tab is in the menu", page.is_visible("#maintab-chart"))
        page.click("#maintab-chart"); page.wait_for_timeout(300)
        check("Chart tab opens with a pair list", page.is_visible("#chart-pair-select") and not page.is_visible("#chart-wrap"))
        opts = page.eval_on_selector_all("#chart-pair-select option", "els=>els.map(e=>e.value)")
        key = next((o for o in opts if o.endswith("::TESTUSDT")), "")
        check("the pair is in the list", bool(key), str(opts))
        page.select_option("#chart-pair-select", key); page.wait_for_timeout(900)
        check("chart shows after choosing the pair", page.is_visible("#chart-wrap") and page.is_visible("#chart-price") and page.is_visible("#chart-shadow"))
        check("status line names the pair and timeframe", "TESTUSDT" in page.inner_text("#chart-status") and "1h" in page.inner_text("#chart-status"), page.inner_text("#chart-status"))
        ink, _ = page.evaluate(INK, "chart-price")
        check("real candles are drawn on the top chart", ink > 5000, str(ink))
        sink, orange = page.evaluate(INK, "chart-shadow")
        check("the shadow tracker line is drawn below (orange)", sink > 1000 and orange > 50, f"{sink} {orange}")
        d = page.evaluate("({n: CH.data.candles.length, real: CH.data.real_markers.length, sh: CH.data.shadow_markers.length, lv: CH.data.levels.map(l => l.label), line: CH.data.shadow_line.length})")
        check("data: candles, real and shadow trades, levels, shadow line", d["n"] == 300 and d["real"] == 2 and d["sh"] == 2 and d["lv"] == ["Stop Loss (on the exchange)", "Max Loss per Trade"] and d["line"] == 200, str(d))
        top = page.evaluate("document.getElementById('chart-price').getBoundingClientRect().bottom")
        bot = page.evaluate("document.getElementById('chart-shadow').getBoundingClientRect().top")
        check("shadow chart is below the price chart", bot > top)
        w1 = page.evaluate("document.getElementById('chart-price').getBoundingClientRect().width")
        w2 = page.evaluate("document.getElementById('chart-shadow').getBoundingClientRect().width")
        check("both charts have the same width (same time line)", abs(w1 - w2) < 1, f"{w1} {w2}")
        box = page.locator("#chart-price").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + 100); page.wait_for_timeout(150)
        tip = page.inner_text("#chart-tip") if page.is_visible("#chart-tip") else ""
        check("hover shows the candle and the shadow value at that time", " O " in tip and " C " in tip and "shadow" in tip, tip)
        n0 = page.evaluate("CH.n")
        page.click("#chart-zoom-in"); page.wait_for_timeout(100)
        n1 = page.evaluate("CH.n")
        page.click("#chart-zoom-out"); page.click("#chart-zoom-out"); page.wait_for_timeout(100)
        n2 = page.evaluate("CH.n")
        check("+ zooms in, - zooms out", n1 < n0 < n2, f"{n0} {n1} {n2}")
        page.mouse.move(box["x"] + 300, box["y"] + 200); page.mouse.down(); page.mouse.move(box["x"] + 700, box["y"] + 200, steps=5); page.mouse.up()
        page.wait_for_timeout(100)
        off = page.evaluate("CH.off")
        check("dragging moves back in time", off > 0, str(off))
        page.click("#chart-latest"); page.wait_for_timeout(100)
        check("Latest goes back to the newest candle", page.evaluate("CH.off") == 0)
        calls = len(CHART_CALLS)
        page.wait_for_timeout(5600)
        check("it updates by itself every 5 seconds while the tab is open", len(CHART_CALLS) > calls, f"{calls} -> {len(CHART_CALLS)}")
        page.click("#maintab-ledger"); page.wait_for_timeout(100)
        check("leaving the tab stops the updates", page.evaluate("CH.timer") is None)
        page.click("#maintab-chart"); page.wait_for_timeout(700)
        icons = page.evaluate("[...document.querySelectorAll('#tabpage-chart .info-i')].map(e => e.dataset.help)")
        check("Chart tab has its i icons with texts", len(icons) >= 4 and all(page.evaluate(f"!!HELP['{k}']") for k in icons), str(icons))
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(300)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        ink2, _ = page.evaluate(INK, "chart-price")
        check("phone: the chart is redrawn to fit", ink2 > 2000 and page.evaluate("document.getElementById('chart-price').getBoundingClientRect().height") == 300)
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
