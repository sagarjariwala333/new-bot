"""
browser_leverage_boost_check.py - OPTIONAL real-browser check of Higher leverage on a proven divergence (2026-11-19)
in the Hub Indicators tab. Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_leverage_boost_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_leverage_boost")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_macd={"entry": True}, name="Boost test")
for _b in ("mfi", "cci", "stoch", "cmo", "obv"):
    PLAN1["ind"].pop(_b, None)                 # an older plan: these indicators did not exist
for _b in PLAN1["ind"].values():
    _b.pop("tf", None)                         # and no timeframe saved
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




def apply_review(page):
    page.wait_for_timeout(150)
    if page.is_visible("#hub-startas-box") and not page.is_checked("#startas-follow") and not page.is_checked("#startas-new"):
        page.click("#startas-follow"); page.wait_for_timeout(120)
    page.click("#hub-review-apply")


def only(page, fid, value):
    for x in page.query_selector_all(f"#hub_{fid} input[type=checkbox]"):
        want = x.get_attribute("value") == value
        if x.is_checked() != want:
            x.click()
    page.wait_for_timeout(120)


def tick(page, fid, value, on=True):
    for x in page.query_selector_all(f"#hub_{fid} input[type=checkbox]"):
        if x.get_attribute("value") == value and x.is_checked() != on:
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
    def wait(ms=200): page.wait_for_timeout(ms)
    def vis(i): return page.is_visible("#hub_" + i)
    def plan_of(sym): return store.get_account(acc.id).pairs[sym].plan
    seen = {"review": ""}
    def save():
        page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            seen["review"] = page.inner_text("#dlg-hub-review")
            apply_review(page); wait(800)
    def open_pair():
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        wait(250)
    def pick_boost(on):
        """The booster is its own menu function now (2026-11-21): pick / unpick it in the menu, then open it."""
        page.click("#hub-edit-btn"); wait(150)
        pressed = page.get_attribute('#hub-menu button[data-fn="boost"]', "aria-pressed") == "true"
        if pressed != on:
            page.click('#hub-menu button[data-fn="boost"]'); wait(100)
        page.click("#hub-confirm-btn"); wait(250)
        if page.is_visible("#dlg-hub-remind"): page.click("#hub-remind-ok"); wait(80)
        if on:
            page.click('.hubx-item[data-fn="boost"]'); wait(200)
    def blk(k): return ((plan_of("AAAUSDT") or {}).get("ind") or {}).get(k, {})
    TFS = ["Chart", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "2d", "3d", "5d", "1w", "1M"]
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        warns = []; page.on("console", lambda m: warns.append(m.text) if "No help text" in m.text else None)
        page.goto(base); wait(600)
        open_pair()
        bz = lambda: ((plan_of("AAAUSDT") or {}).get("sizing") or {}).get("boost")
        page.click("#hub-edit-btn"); wait(150)
        check("it is its own function in the menu, not picked", page.get_attribute('#hub-menu button[data-fn="boost"]', "aria-pressed") != "true"
              and "Higher leverage on a divergence" in page.inner_text('#hub-menu button[data-fn="boost"]'))
        page.click("#hub-confirm-btn"); wait(200)
        check("not picked: no box showing and nothing saved as on", not page.is_checked("#hub_boost_on") and not vis("boost_leverage") and not vis("bzdiv_combine") and not vis("bzdiv_rsi_on")
              and "on" not in (page.get_attribute('.hubx-item[data-fn="boost"]', "class") or "").split())
        check("it is not inside Entry size any more", page.query_selector('#hubpane_size #hub_boost_leverage') is None and page.query_selector('#hubpane_boost #hub_boost_leverage') is not None)
        pick_boost(True)
        check("picked: the leverage box starts EMPTY", page.input_value("#hub_boost_leverage") == "", page.input_value("#hub_boost_leverage"))
        check("ticked: leverage, combine, valid candles, turn-back, price-break and the 7 oscillators show",
              all(vis(x) for x in ("boost_leverage", "bzdiv_combine", "bzdiv_keep", "bzdiv_turn", "bzdiv_price"))
              and all(vis(f"bzdiv_{k}_on") for k in ("rsi", "mfi", "cci", "macd", "stoch", "obv", "cmo")))
        check("an oscillator's boxes show only when it is ticked", not vis("bzdiv_rsi_tf") and not vis("bzdiv_rsi_length"))
        page.check("#hub_bzdiv_rsi_on"); wait(200)
        check("RSI ticked: timeframe, length, pivot, look-back show", all(vis(f"bzdiv_rsi_{k}") for k in ("tf", "length", "pivot", "lookback")))
        page.fill("#hub_boost_leverage", "8")
        page.select_option("#hub_bzdiv_combine", "ANY"); page.fill("#hub_bzdiv_keep", "6")
        page.select_option("#hub_bzdiv_rsi_tf", "3d")
        page.fill("#hub_bzdiv_rsi_length", "14"); page.fill("#hub_bzdiv_rsi_pivot", "3"); page.fill("#hub_bzdiv_rsi_lookback", "30")
        save()
        x = bz() or {}
        r = (x.get("div") or {}).get("rsi") or {}
        check("saved: on, leverage 8, ANY, 6 candles, RSI on 3d with its numbers",
              (x.get("on"), x.get("leverage"), x["div"]["combine"], x["div"]["keep"], r.get("on"), r.get("tf"), r.get("length"), r.get("pivot"), r.get("lookback"))
              == (True, 8, "ANY", 6, True, "3d", 14, 3, 30), str(x) + page.inner_text("#hub-msg")[:200])
        check("the review shows it", "Higher leverage on divergence" in seen["review"] and "RSI on 3d" in seen["review"], seen["review"].replace("\n", " | ")[200:900])
        check("the normal leverage is untouched", ((plan_of("AAAUSDT") or {}).get("sizing") or {}).get("leverage") == 5, str((plan_of("AAAUSDT") or {}).get("sizing")))
        # refused cases
        page.fill("#hub_boost_leverage", "0"); save()
        check("leverage 0 is refused", page.inner_text("#hub-msg").startswith("Not saved"), page.inner_text("#hub-msg")[:200])
        page.fill("#hub_boost_leverage", "8")
        page.uncheck("#hub_bzdiv_rsi_on"); save()
        check("no oscillator ticked is refused", page.inner_text("#hub-msg").startswith("Not saved"), page.inner_text("#hub-msg")[:200])
        page.check("#hub_bzdiv_rsi_on"); wait(150)
        page.check("#hub_bzdiv_cci_on"); wait(150)
        page.select_option("#hub_bzdiv_cci_tf", "Chart")
        page.fill("#hub_bzdiv_cci_length", "20"); page.fill("#hub_bzdiv_cci_pivot", "3"); page.fill("#hub_bzdiv_cci_lookback", "30")
        page.select_option("#hub_bzdiv_combine", "ALL")
        save()
        x = bz() or {}
        check("saved: two oscillators, ALL", (x.get("div") or {}).get("combine") == "ALL" and x["div"]["cci"]["on"] is True and x["div"]["rsi"]["on"] is True, str(x) + page.inner_text("#hub-msg")[:200])
        page.reload(); wait(600)
        open_pair()
        check("after a reload everything is back",
              page.is_checked("#hub_boost_on") and page.input_value("#hub_boost_leverage") == "8" and page.input_value("#hub_bzdiv_rsi_tf") == "3d"
              and page.input_value("#hub_bzdiv_combine") == "ALL" and page.input_value("#hub_bzdiv_keep") == "6")
        pick_boost(False); save()
        check("unticked: saved as off, nothing else kept", (bz() or {}).get("on") is False and "div" not in (bz() or {}), str(bz()) + page.inner_text("#hub-msg")[:200])
        check("no JavaScript errors", not errors, str(errors[:3]))
        check("no 'No help text' warning", not warns, str(warns[:3]))
        b.close()
    ok = all(res)
    print(f"\n{sum(res)} passed, {len(res) - sum(res)} failed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
