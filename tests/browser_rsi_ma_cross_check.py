"""
browser_rsi_ma_cross_check.py - OPTIONAL real-browser check of the RSI crosses its own MA condition (2026-11-01)
in the Hub Indicators tab. Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_rsi_ma_cross_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_rsi_ma_cross")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_macd={"entry": True}, name="RSI MA cross test")
PLAN1["ind"]["rsi"].pop("mode", None)          # an older plan: no Condition saved
PLAN1["ind"]["macd"].pop("mode", None)
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
    """Condition ticks: tick exactly this one condition (real clicks)."""
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


def ticked(page, fid):
    return page.eval_on_selector_all(f"#hub_{fid} input[type=checkbox]", "xs => xs.filter(x => x.checked).map(x => x.value)")


CROSS = "RSI crosses its own MA"


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
            page.click("#hub-review-apply"); wait(800)
    def open_pair():
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        page.click('.hubx-item[data-fn="ind"]'); wait(150)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        open_pair()
        modes = page.eval_on_selector_all("#hub_rsi_modes input[type=checkbox]", "xs => xs.map(x => x.value)")
        check("RSI lists 4 conditions, the new one last",
              modes == ["Above / below level", "Overbought / oversold", "Divergence", CROSS], str(modes))
        check("an older plan still shows Above / below level", ticked(page, "rsi_modes") == ["Above / below level"])
        check("new boxes are hidden while the condition is not ticked",
              not any(vis(x) for x in ("rsi_ma_type", "rsi_ma_length", "rsi_ma_alma_offset", "rsi_ma_alma_sigma")))
        only(page, "rsi_modes", CROSS)
        check("only the new condition: RSI Length, average type, average length and N show",
              vis("rsi_length") and vis("rsi_ma_type") and vis("rsi_ma_length") and vis("rsi_n"))
        check("the other conditions' boxes are hidden",
              not any(vis(x) for x in ("rsi_level", "rsi_oversold", "rsi_overbought", "rsi_pivot", "rsi_lookback")))
        check("ALMA boxes are hidden until the type is ALMA", not vis("rsi_ma_alma_offset") and not vis("rsi_ma_alma_sigma"))
        opts = page.eval_on_selector_all("#hub_rsi_ma_type option", "os => os.map(o => o.value)")
        check("type list = SMA, EMA, WMA, HMA, ALMA (plus the empty pick)",
              [o for o in opts if o] == ["SMA", "EMA", "WMA", "HMA", "ALMA"], str(opts))
        check("nothing is filled in for the owner (type, length, N empty)",
              page.input_value("#hub_rsi_ma_type") == "" and page.input_value("#hub_rsi_ma_length") == ""
              and page.input_value("#hub_rsi_n") == "", f"{page.input_value('#hub_rsi_ma_type')!r}")
        page.fill("#hub_rsi_length", "14")
        save()
        msg = page.inner_text("#hub-msg")
        check("empty average boxes: Not saved", msg.startswith("Not saved"), msg[:200])
        page.select_option("#hub_rsi_ma_type", "ALMA"); wait(150)
        check("ALMA picked: offset and sigma boxes show", vis("rsi_ma_alma_offset") and vis("rsi_ma_alma_sigma"))
        page.select_option("#hub_rsi_ma_type", "EMA"); wait(150)
        check("EMA picked: ALMA boxes go away", not vis("rsi_ma_alma_offset") and not vis("rsi_ma_alma_sigma"))
        page.fill("#hub_rsi_ma_length", "9"); page.fill("#hub_rsi_n", "2")
        save()
        r = ((plan_of("AAAUSDT") or {}).get("ind") or {}).get("rsi", {})
        check("saved: crosses its own MA, EMA, length 9, N 2, RSI length 14",
              (r.get("modes"), r.get("ma_type"), r.get("ma_length"), r.get("n"), r.get("length"))
              == ([CROSS], "EMA", 9, 2, 14), str(r) + page.inner_text("#hub-msg")[:200])
        check("saved plan holds no ALMA numbers and none of the other conditions' numbers",
              not any(k in r for k in ("ma_alma_offset", "ma_alma_sigma", "level", "oversold", "overbought", "pivot", "lookback")), str(r))
        print("REVIEW TEXT >>>", seen["review"].replace("\n", " | "))
        page.select_option("#hub_rsi_ma_type", "ALMA"); wait(150)
        page.fill("#hub_rsi_ma_alma_offset", "0.85"); page.fill("#hub_rsi_ma_alma_sigma", "6")
        save()
        r = ((plan_of("AAAUSDT") or {}).get("ind") or {}).get("rsi", {})
        check("saved: ALMA with its two numbers",
              (r.get("ma_type"), r.get("ma_alma_offset"), r.get("ma_alma_sigma")) == ("ALMA", 0.85, 6), str(r))
        page.fill("#hub_rsi_ma_alma_sigma", "0"); save()
        msg = page.inner_text("#hub-msg")
        check("ALMA sigma 0 is refused", msg.startswith("Not saved"), msg[:200])
        page.fill("#hub_rsi_ma_alma_sigma", "6")
        page.fill("#hub_rsi_ma_length", "0"); save()
        msg = page.inner_text("#hub-msg")
        check("average length 0 is refused", msg.startswith("Not saved"), msg[:200])
        page.fill("#hub_rsi_ma_length", "9")
        tick(page, "rsi_modes", "Above / below level")
        check("ticked together with Above / below level: both sets of boxes show",
              vis("rsi_level") and vis("rsi_ma_type") and vis("rsi_ma_length") and vis("rsi_n"))
        page.fill("#hub_rsi_level", "50"); save()
        r = ((plan_of("AAAUSDT") or {}).get("ind") or {}).get("rsi", {})
        check("saved: both conditions ticked",
              sorted(r.get("modes") or []) == sorted(["Above / below level", CROSS]) and r.get("level") == 50, str(r))
        tick(page, "rsi_modes", "Above / below level", on=False)
        save()
        r = ((plan_of("AAAUSDT") or {}).get("ind") or {}).get("rsi", {})
        check("saved again with only the new condition", r.get("modes") == [CROSS] and "level" not in r, str(r))
        # reload the page: the saved values come back
        page.reload(); wait(600)
        open_pair()
        check("after a reload: the condition, type, lengths and ALMA numbers are back",
              ticked(page, "rsi_modes") == [CROSS] and page.input_value("#hub_rsi_ma_type") == "ALMA"
              and page.input_value("#hub_rsi_ma_length") == "9" and page.input_value("#hub_rsi_ma_alma_offset") == "0.85"
              and page.input_value("#hub_rsi_ma_alma_sigma") == "6" and page.input_value("#hub_rsi_length") == "14",
              f"{ticked(page, 'rsi_modes')} {page.input_value('#hub_rsi_ma_type')}")
        for key in ("rsi_modes", "rsi_ma_type", "rsi_ma_length", "rsi_ma_alma_offset", "rsi_ma_alma_sigma", "rsi_n"):
            el = page.query_selector(f'.info-i[data-help="{key}"]')
            ok = el is not None and el.is_visible()
            if ok:
                el.click(); wait(150); ok = page.is_visible("#dlg-help"); txt = page.inner_text("#dlg-help") if ok else ""
                ok = ok and len(txt) > 20
                page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens its explanation", ok)
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
