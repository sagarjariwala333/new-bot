"""
browser_eb_sar_check.py - OPTIONAL real-browser check of the 4th Trailing Stop type "Signal candle high / low +/- ATR"
(2026-10-28): Distance (% of ATR) and ATR length, the review line and the i texts.
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_trail_candle_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_eb_sar")
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
PLAN1 = make_plan(ind_ma1={"entry": True}, name="Band SAR")
del PLAN1["ind"]["eb"], PLAN1["ind"]["sar"]          # a plan saved before these two blocks existed
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
    def tick(i):
        page.check("#hub_" + i); wait(150); dismiss()
    def untick(i):
        page.uncheck("#hub_" + i); wait(150); dismiss()
    def save():
        page.click("#hub-save-btn"); wait(400)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); wait(100); page.click("#hub-save-btn"); wait(400)
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
        page.click('.hubx-item[data-fn="ind"]'); wait(200)

        for blk in ("eb", "sar"):
            check(f"{blk}: row has ENTRY, FLIP and CTREND ticks", all(page.query_selector(f"#hub_{blk}_{t}") is not None for t in ("entry", "flip", "ctrend")))
            check(f"{blk}: nothing ticked and its boxes are hidden", not page.is_checked(f"#hub_{blk}_entry") and not vis(f"{blk}_length" if blk == "eb" else f"{blk}_step"))
        check("indicator table now has 16 rows", page.evaluate("document.querySelectorAll('.hubx-trow:not(.hubx-thead)').length") == 16)

        # ---------------- EMA Band
        tick("eb_entry")
        check("EMA Band boxes show: length, N and the 4 crossings", all(vis(x) for x in ("eb_length", "eb_n", "eb_long_low", "eb_long_high", "eb_short_high", "eb_short_low")))
        check("nothing is preset: numbers empty, crossings unticked",
              page.input_value("#hub_eb_length") == "" and page.input_value("#hub_eb_n") == "" and not any(page.is_checked("#hub_eb_" + k) for k in ("long_low", "long_high", "short_high", "short_low")))
        save()
        msg = page.inner_text("#hub-msg")
        check("empty numbers: refused, nothing saved", msg.startswith("Not saved") and not ((plan_of("AAAUSDT").get("ind") or {}).get("eb") or {}).get("entry"), msg[:200])
        page.fill("#hub_eb_length", "8"); page.fill("#hub_eb_n", "2")
        save()
        msg = page.inner_text("#hub-msg")
        check("numbers but no crossing ticked: refused", msg.startswith("Not saved") and "crossing" in msg, msg[:200])
        tick("eb_long_low"); tick("eb_short_high")
        save()
        eb = (plan_of("AAAUSDT").get("ind") or {}).get("eb") or {}
        check("saved: entry, length 8, N 2, long_low and short_high only",
              (eb.get("entry"), eb.get("length"), eb.get("n"), eb.get("long_low"), eb.get("long_high"), eb.get("short_high"), eb.get("short_low")) == (True, 8, 2, True, False, True, False),
              str(eb) + page.inner_text("#hub-msg")[:200])
        tick("eb_long_high")
        save()
        eb = (plan_of("AAAUSDT").get("ind") or {}).get("eb") or {}
        check("both long lines can be ticked", eb.get("long_low") is True and eb.get("long_high") is True, str(eb))

        # ---------------- Parabolic SAR
        tick("sar_entry")
        check("SAR boxes show and are empty", vis("sar_step") and vis("sar_limit") and page.input_value("#hub_sar_step") == "" and page.input_value("#hub_sar_limit") == "")
        page.fill("#hub_sar_step", "0.2"); page.fill("#hub_sar_limit", "0.1")
        save()
        msg = page.inner_text("#hub-msg")
        check("maximum smaller than the step: refused", msg.startswith("Not saved") and "maximum" in msg.lower(), msg[:200])
        page.fill("#hub_sar_step", "0.02"); page.fill("#hub_sar_limit", "0.2")
        save()
        sar = (plan_of("AAAUSDT").get("ind") or {}).get("sar") or {}
        check("saved: entry, step 0.02, maximum 0.2", (sar.get("entry"), sar.get("step"), sar.get("limit")) == (True, 0.02, 0.2), str(sar) + page.inner_text("#hub-msg")[:200])

        # ---------------- counter trend size
        tick("eb_ctrend")
        page.click('.hubx-item[data-fn="size"]'); wait(200)
        txt = page.eval_on_selector("#hub_counter_by", "e => e.options[e.selectedIndex].text") if page.query_selector("#hub_counter_by") else ""
        line = page.inner_text("#hub_counter_line") if page.query_selector("#hub_counter_line") else ""
        check("Counter trend 'decided by' shows the EMA Band", "EMA Band" in (txt or "") or "EMA Band" in line, txt + "|" + line)
        page.click('.hubx-item[data-fn="ind"]'); wait(200)
        untick("eb_ctrend"); tick("sar_ctrend")
        page.click('.hubx-item[data-fn="size"]'); wait(200)
        line = page.inner_text("#hub_counter_line") if page.query_selector("#hub_counter_line") else ""
        check("...and Parabolic SAR when that one is ticked", "Parabolic SAR" in line, line)
        page.click('.hubx-item[data-fn="ind"]'); wait(200)

        # ---------------- reload keeps everything
        page.click("#maintab-hub"); wait(300)
        page.reload(); wait(700)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        page.click('.hubx-item[data-fn="ind"]'); wait(200)
        check("after reopening the pair: boxes keep their values",
              page.input_value("#hub_eb_length") == "8" and page.input_value("#hub_eb_n") == "2" and page.is_checked("#hub_eb_long_high")
              and page.input_value("#hub_sar_step") == "0.02" and page.input_value("#hub_sar_limit") == "0.2")

        for key in ("ind.eb", "ind.sar", "eb_length", "eb_n", "eb_long_low", "eb_long_high", "eb_short_high", "eb_short_low", "sar_step", "sar_limit"):
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
