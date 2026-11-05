"""
browser_sr_check.py - OPTIONAL real-browser check of Support / Resistance (2026-11-05): the menu function, the Indicators row,
saving, nothing pre-selected, the chart lines and the levels table.
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_sr_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_sr")
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
SR1 = {"on": True, "find_hhll": True, "hhll_n": 30, "find_swing": True, "swing_n": 3, "diagonal": True, "diag_points": 2, "count": 2,
       "zone_on": True, "zone_atr_pct": 20.0, "stop_zone_on": True, "stop_zone_atr_pct": 15.0, "atr_length": 14,
       "bounce_long": True, "bounce_short": True, "bounce_edges": True, "bounce_edge_pct": 30.0,
       "bx_other": True, "bx_break": True, "brk_long": True, "brk_short": True, "brk_close_on": True, "brk_close_atr_pct": 10.0,
       "brk_vol_on": True, "brk_vol_mult": 1.5, "brk_vol_len": 20, "brk_exit_inside": True,
       "retest_long": True, "retest_short": True, "wait_fail": True, "wait_newrange": True, "wait_runaway": True,
       "wait_runaway_atr_pct": 300.0, "alone": True, "exit_sr_only": True}
PLAN1 = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True}, sr=dict(SR1), name="SR plan")
store.add_pair(acc.id, tv.pair_config("AAAUSDT", plan=PLAN1, timeframe="1h"))
store.add_pair(acc.id, tv.pair_config("BBBUSDT", plan=None, timeframe="1h"))

# ---- chart data: the real route and the real calculation, fake candles
import math as _math
from app import chart_api
import unittest.mock as _mock


class _Klines:
    async def get_klines(self, symbol, interval, limit=500):
        rows, p = [], 100.0
        for k in range(1300):
            o = p
            p = 100 + 6 * _math.sin(k / 9.0) + 2 * _math.sin(k / 3.1)
            rows.append([1_700_000_000_000 + k * 3_600_000, str(o), str(max(o, p) + 0.5), str(min(o, p) - 0.5), str(p),
                         str(100 + (k % 7) * 20), 0, 0, 0, 0, 0, 0])
        return rows[-limit:]

    async def close(self):
        pass


class _PC:
    timeframe = "1h"
    plan = PLAN1


_mock.patch.object(chart_api, "_pair_and_client", side_effect=lambda *a: (_PC, _Klines(), False)).start()


class Body:
    def __init__(self, d): self.__dict__.update(d)


def route(method, path, payload):
    try:
        if path.startswith("/api/chart/"):
            parts = path.split("/")
            return 200, asyncio.run(chart_api.chart_data(parts[3], parts[4], parts[5], 500))
        if path == "/api/auth/status": return 200, {"ok": True}
        if path == "/api/accounts": return 200, api.list_accounts()
        if path == "/api/okx/accounts": return 200, okx_api.list_okx_accounts()
        if path in ("/api/status", "/api/okx/status"): return 200, {"instances": [], "startup_failures": {}}
        if path == "/healthz": return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/default-settings": return 200, api.get_default_settings()
        if path == "/api/library" and method == "GET": return 200, asyncio.run(api.get_library())
        if path == "/api/library" and method == "POST": return 200, asyncio.run(api.save_to_library(Body(payload)))
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


SR_IDS = ["sr_hhll", "sr_hhll_n", "sr_swing", "sr_swing_n", "sr_diag", "sr_diag_points", "sr_count", "sr_zone_on", "sr_zone_pct",
          "sr_stop_on", "sr_stop_pct", "sr_atr_length", "sr_bounce_long", "sr_bounce_short", "sr_bounce_edges", "sr_edge_pct",
          "sr_bx_other", "sr_bx_mid", "sr_bx_break", "sr_brk_long", "sr_brk_short", "sr_brk_close_on", "sr_brk_close_pct",
          "sr_brk_vol_on", "sr_brk_vol_mult", "sr_brk_vol_len", "sr_brk_exit_inside", "sr_retest_long", "sr_retest_short",
          "sr_wait_fail", "sr_wait_newrange", "sr_wait_runaway", "sr_wait_runaway_pct", "sr_wait_none", "sr_alone",
          "sr_exit_sr_only", "sr_exit_all"]
SR_TICKS = [i for i in SR_IDS if not any(i.endswith(x) for x in ("_n", "_points", "_count", "_pct", "_mult", "_len", "_length"))]


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
    def open_pair(sym, first=False):
        page.click("#maintab-hub"); wait(300)
        if first:
            page.select_option("#hub-acct-select", index=1); wait(300)
            page.click("#hub-confirm-btn"); wait(200)
        else:
            fn("pair")
        page.select_option("#hub-pair-select", sym); wait(500)
        if page.is_visible("#hub-confirm-btn"):
            page.click("#hub-confirm-btn"); wait(200)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        open_pair("AAAUSDT", first=True)
        items = page.eval_on_selector_all(".hubx-item", "e => e.map(x => x.getAttribute('data-fn'))")
        check("Support / Resistance is a function of the menu (the last one)", items and items[-1] == "sr", str(items))
        check("it is picked, because the saved plan uses it", page.evaluate("hubUi.picked.sr === true"))
        fn("sr")
        check("the pane has its title", "Support / Resistance" in page.inner_text("#hubtitle_sr"))
        titles = page.eval_on_selector_all("#hubpane_sr .hubx-sub", "e => e.map(x => x.innerText.replace(/\\s*i$/, '').trim())")
        check("the groups are in capitals, in order", titles == ["FIND THE RANGE", "ZONE", "ENTRY BOUNCE", "EXIT BOUNCE", "ENTRY BREAKOUT",
                                                              "EXIT BREAKOUT", "ENTRY RETEST", "WHAT ENDS THE RETEST WAIT", "USE IN THE STRATEGY"], str(titles))
        # saved values load
        ok = True; bad = []
        for key, fid in (("find_hhll", "sr_hhll"), ("find_swing", "sr_swing"), ("diagonal", "sr_diag"), ("zone_on", "sr_zone_on"),
                         ("bounce_edges", "sr_bounce_edges"), ("brk_exit_inside", "sr_brk_exit_inside"), ("wait_runaway", "sr_wait_runaway"),
                         ("alone", "sr_alone"), ("exit_sr_only", "sr_exit_sr_only")):
            if not page.is_checked("#hub_" + fid): ok = False; bad.append(fid)
        check("saved ticks load as ticked", ok, str(bad))
        check("unticked ones load as unticked", not page.is_checked("#hub_sr_bx_mid") and not page.is_checked("#hub_sr_wait_none") and not page.is_checked("#hub_sr_exit_all"))
        check("saved numbers load", (page.input_value("#hub_sr_hhll_n"), page.input_value("#hub_sr_swing_n"), page.input_value("#hub_sr_count"),
                                     page.input_value("#hub_sr_edge_pct"), page.input_value("#hub_sr_brk_vol_len")) == ("30", "3", "2", "30", "20"))
        # boxes appear only with their tick
        page.uncheck("#hub_sr_diag"); wait(100)
        check("untick Diagonal: its point count hides, swing N stays (swing points still ticked)", not vis("sr_diag_points") and vis("sr_swing_n"))
        page.uncheck("#hub_sr_swing"); wait(100)
        check("untick Swing points too: swing N hides", not vis("sr_swing_n"))
        page.check("#hub_sr_diag"); wait(100)
        check("tick Diagonal alone: swing N shows again", vis("sr_swing_n") and vis("sr_diag_points"))
        page.check("#hub_sr_swing"); wait(100)
        page.uncheck("#hub_sr_hhll"); wait(100)
        check("untick Highest high: N candles hides", not vis("sr_hhll_n"))
        page.check("#hub_sr_hhll"); wait(100)
        for tick, box in (("sr_zone_on", "sr_zone_pct"), ("sr_stop_on", "sr_stop_pct"), ("sr_bounce_edges", "sr_edge_pct"),
                          ("sr_brk_close_on", "sr_brk_close_pct"), ("sr_brk_vol_on", "sr_brk_vol_mult"), ("sr_wait_runaway", "sr_wait_runaway_pct")):
            page.uncheck("#hub_" + tick); wait(60)
            hidden = not vis(box)
            page.check("#hub_" + tick); wait(60)
            check(f"{box}: hidden without {tick}, shown with it", hidden and vis(box))
        for t in ("sr_zone_on", "sr_stop_on", "sr_brk_close_on", "sr_wait_runaway"): page.uncheck("#hub_" + t)
        wait(80)
        check("ATR length hides when no ATR tick is on", not vis("sr_atr_length"))
        page.check("#hub_sr_stop_on"); wait(80)
        check("ATR length shows when any ATR tick is on", vis("sr_atr_length"))
        for t in ("sr_zone_on", "sr_brk_close_on", "sr_wait_runaway"): page.check("#hub_" + t)
        wait(80)
        # change and save
        page.fill("#hub_sr_count", "3"); page.fill("#hub_sr_zone_pct", "35"); page.check("#hub_sr_bx_mid"); page.check("#hub_sr_exit_all")
        save()
        sr = (plan_of("AAAUSDT") or {}).get("sr") or {}
        check("saved: the plan holds the Support / Resistance settings", sr.get("on") is True and sr.get("count") == 3 and sr.get("zone_atr_pct") == 35
              and sr.get("bx_mid") is True and sr.get("exit_all") is True and sr.get("alone") is True and sr.get("hhll_n") == 30,
              str(sr) + page.inner_text("#hub-msg")[:200])
        pl = plan_of("AAAUSDT") or {}
        check("saved: SR is ticked in the Indicators list too", pl.get("ind", {}).get("sr", {}).get("entry") is True)
        # a refused plan shows the reason
        page.uncheck("#hub_sr_hhll"); page.uncheck("#hub_sr_swing"); page.uncheck("#hub_sr_diag"); wait(100)
        dismiss(); page.click("#hub-save-btn"); wait(500)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        msg = page.inner_text("#hub-msg")
        check("nothing to find the range with: the bot's reason is shown", "find the range" in msg, msg[:300])
        page.check("#hub_sr_hhll"); page.check("#hub_sr_swing"); page.check("#hub_sr_diag"); wait(100)
        # review lines
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review shows the Support / Resistance lines", "Support / Resistance: finds the range" in rv and "Entries:" in rv, rv[:1200])
        if page.is_visible("#dlg-hub-review"): page.click("#dlg-hub-review button.ghost"); wait(100)
        # ---- a new pair: nothing pre-selected
        page.reload(); wait(700)                      # a fresh page: nothing typed before
        open_pair("BBBUSDT", first=True)
        page.evaluate("hubUi.picked.sr = true; hubRefresh();"); wait(200)
        fn("sr")
        some = [i for i in SR_TICKS if page.is_checked("#hub_" + i)] if all(page.query_selector("#hub_" + i) for i in SR_TICKS) else ["missing"]
        check("new pair: not one Support / Resistance tick is pre-selected", some == [], str(some))
        nums = [i for i in SR_IDS if i not in SR_TICKS and page.input_value("#hub_" + i) != ""]
        check("new pair: not one Support / Resistance number is pre-filled", nums == [], str(nums))
        check("new pair: only the always-visible boxes show (no hidden number leaks)", not vis("sr_hhll_n") and not vis("sr_zone_pct") and not vis("sr_wait_runaway_pct"))
        # ---- Indicators row
        fn("ind")
        check("Support / Resistance has an ENTRY and a FLIP tick in Indicators", page.query_selector("#hub_sr_entry") and page.query_selector("#hub_sr_flip"))
        check("and no CTREND tick", page.query_selector("#hub_sr_ctrend") is None)
        page.evaluate("hubUi.picked.sr = false; hubRefresh();"); wait(100)
        check("function not picked: its Indicators ticks are cleared", not page.is_checked("#hub_sr_entry"))
        page.check("#hub_sr_entry"); wait(150); dismiss()
        check("ticking it in Indicators picks the function for you", page.evaluate("hubUi.picked.sr === true"))
        check("and its card shows a pointer to the function", page.is_visible("#hubcard_sr"))
        main_opts = page.eval_on_selector_all("#hub_main_entry option", "o => o.map(x => x.textContent)")
        check("it can be chosen as the Main", any("Support / Resistance" in x for x in main_opts), str(main_opts))
        # ---- help texts
        missing = page.evaluate("ids => ids.filter(k => !(typeof HELP !== 'undefined' && HELP[k]))", SR_IDS + ["fn.sr", "ind.sr", "chart.sr", "chart.srtable"] +
                                ["card." + c for c in ("srfind", "srzone", "srbounce", "srbx", "srbrk", "srbx2", "srretest", "srwait", "sruse")])
        check("every new box, group and chart part has a help text", missing == [], str(missing))
        # ---- chart tab
        page.click("#maintab-chart"); wait(300)
        opts = page.eval_on_selector_all("#chart-pair-select option", "els=>els.map(e=>e.value)")
        key = next((o for o in opts if o.endswith("::AAAUSDT")), "")
        page.select_option("#chart-pair-select", key); wait(1500)
        d = page.evaluate("CH.data.sr")
        check("chart data: Support / Resistance is on with lines, signals and a table", d and d["on"] and d["lines"]["s1"] and len(d["lines"]["s1"]) == 500 and len(d["markers"]) > 0 and len(d["table"]) >= 2, str(d)[:300])
        check("the legend shows", page.is_visible("#chart-sr-legend"))
        check("the table shows", page.is_visible("#chart-sr") and page.is_visible("#chart-sr-table"))
        heads = page.eval_on_selector_all("#chart-sr-table th", "e => e.map(x => x.innerText)")
        check("all 8 columns are shown at first", len(heads) == 8, str(heads))
        rows = page.eval_on_selector_all("#chart-sr-table tbody tr", "e => e.map(x => x.innerText)")
        check("rows: Support / Resistance names", any("Support 1" in r for r in rows) and any("Resistance 1" in r for r in rows), str(rows[:3]))
        check("flat rows show '—' in the diagonal column", any(r.rstrip().endswith("—") for r in rows), str(rows))
        cell = page.evaluate("chartSrCell('diag', {diagonal: true, price: 100.5, slope: -0.25})")
        check("a diagonal row shows its price now and its slope per candle", "100.5" in cell and "slope" in cell and "0.25" in cell, cell)
        page.uncheck('#chart-sr-cols input[data-srcol="used"]'); wait(150)
        heads2 = page.eval_on_selector_all("#chart-sr-table th", "e => e.map(x => x.innerText)")
        check("untick a column: it goes", len(heads2) == 7 and "Used by" not in " ".join(heads2).title(), str(heads2))
        page.reload(); wait(600)
        page.click("#maintab-chart"); wait(300)
        page.select_option("#chart-pair-select", key); wait(1500)
        heads3 = page.eval_on_selector_all("#chart-sr-table th", "e => e.map(x => x.innerText)")
        check("the column choice is remembered in this browser", len(heads3) == 7, str(heads3))
        INK = """() => { const c = document.getElementById('chart-price'); const g = c.getContext('2d'); const d = g.getImageData(0, 0, c.width, c.height).data;
            let purple = 0, red = 0; for (let i = 0; i < d.length; i += 4) { if (d[i+3] > 0 && d[i] > 150 && d[i+1] < 140 && d[i+2] > 200) purple++; if (d[i] > 240 && d[i+1] > 70 && d[i+1] < 110 && d[i+2] > 100 && d[i+2] < 130) red++; }
            return [purple, red]; }"""
        purple, red = page.evaluate(INK)
        check("lines and signal badges are drawn on the price chart", red > 100, f"{purple} {red}")
        # a pair without it: nothing extra
        _PC.plan = make_plan(ind_ma1={"entry": True})
        page.evaluate("chartLoad()"); wait(1200)
        check("a pair without Support / Resistance: no legend, no table", not page.is_visible("#chart-sr") and not page.is_visible("#chart-sr-legend"))
        _PC.plan = PLAN1
        # a half-saved plan does not break the chart
        _PC.plan = {"sr": {"on": True}}
        page.evaluate("chartLoad()"); wait(1200)
        check("a plan that cannot be worked out shows a message, not an error", page.is_visible("#chart-sr") and "could not be worked out" in page.inner_text("#chart-sr-note"), page.inner_text("#chart-sr-note"))
        _PC.plan = PLAN1
        page.set_viewport_size({"width": 390, "height": 800}); wait(300)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)


if __name__ == "__main__":
    main()
