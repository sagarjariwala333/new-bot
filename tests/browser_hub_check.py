"""
browser_hub_check.py - OPTIONAL real-browser check of the Strategy Hub tab (needs Playwright + Chromium).
Backend = the REAL plan routes of app/api; no exchange calls. Run:  python tests/browser_hub_check.py
"""
import asyncio, json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_hub")
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
    def dismiss():
        # 2026-10-23: changing Max loss / Stop Loss pops up a reminder of the other one's settings
        page.wait_for_timeout(80)
        if page.is_visible("#dlg-hub-remind"):
            page.click("#hub-remind-ok"); page.wait_for_timeout(60)

    def openfn(fn):
        dismiss()
        page.click(f'.hubx-item[data-fn="{fn}"]'); page.wait_for_timeout(120)
    def choose_account_and_pair():
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(200)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); page.wait_for_timeout(600)
        page.click("#maintab-hub"); page.wait_for_timeout(300)
        check("hub tab shows", page.is_visible("#tabpage-hub"))
        check("strategy name box is there", page.is_visible("#hub_name"))
        opts = page.inner_text("#hub-acct-select")
        check("account list has the Binance and the OKX account", "Test" in opts and "OKX: Demo" in opts, opts)
        check("nothing else shows before an account is chosen", not page.is_visible("#hub-form") and page.is_visible("#hub-empty"))
        choose_account_and_pair()
        check("menu + settings show after choosing the account", page.is_visible("#hub-form") and page.is_visible("#hub-menu"))
        names = page.eval_on_selector_all(".hubx-iname", "els=>els.map(e=>e.firstChild.textContent)")   # name only (the "i" icon is a child span)
        check("menu has the 16 functions in order (one tab per function)", names == ["Pair", "Chart type", "Indicators", "Indicator Combination", "On a flip signal", "Hold rule", "Entry wait", "Entry size", "Max Loss per Trade", "Stop Loss", "Break-Even", "Trailing Stop", "Take profit", "Shadow mode", "Cooldown after a loss", "Loss limit"], str(names))
        check("each menu row has its description", all(len(d) > 20 for d in page.eval_on_selector_all(".hubx-idesc", "els=>els.map(e=>e.innerText)")))
        check("pick step: menu title and summary page", "click to pick" in page.inner_text("#hub-menu-title") and page.is_visible("#hub-pick-pane") and not page.is_visible("#hub-fill-pane"))
        check("8 needed functions (Stop Loss is a must, 2026-10-26) + Hold rule (default on, 2026-10-25) are picked", page.inner_text("#hub-progress").startswith("9 picked"))
        page.click('.hubx-item[data-fn="hold"]'); page.wait_for_timeout(100)     # this run unpicks it (picked again later)
        check("8 needed functions are picked, 6 optional are not", page.inner_text("#hub-progress").startswith("8 picked"))
        page.click('.hubx-item[data-fn="pair"]'); page.wait_for_timeout(100)
        check("a needed function cannot be unpicked", page.inner_text("#hub-progress").startswith("8 picked"))
        page.click('.hubx-item[data-fn="maxloss"]'); page.click('.hubx-item[data-fn="tp"]'); page.wait_for_timeout(100)
        check("clicking an optional function picks it", page.inner_text("#hub-progress").startswith("10 picked"))
        page.click('.hubx-item[data-fn="shadow"]'); page.click('.hubx-item[data-fn="shadow"]'); page.wait_for_timeout(100)
        check("clicking again unpicks it", page.inner_text("#hub-progress").startswith("10 picked"))
        check("summary lists what is picked", page.locator(".hubx-srow").count() == 10)
        page.click("#hub-confirm-btn"); page.wait_for_timeout(200)
        check("fill step shows the first function with its pair box", page.is_visible("#hub-fill-pane") and page.is_visible("#hub-pair-select") and not page.is_visible("#hub-pick-pane"))
        check("progress counts filled-in functions", page.inner_text("#hub-progress") == "1 of 10 functions filled in", page.inner_text("#hub-progress"))
        check("Execute is blocked until every function is opened", page.is_disabled("#hub-execute-btn"))
        check("pair list has the pair", "TESTUSDT" in page.inner_text("#hub-pair-select"))
        page.select_option("#hub-pair-select", index=1); page.wait_for_timeout(500)
        check("state line says no plan", "No plan saved" in page.inner_text("#hub-state"))
        # saving before everything is filled
        page.select_option("#hub_timeframe", "")
        page.click("#hub-save-btn"); page.wait_for_timeout(200)
        check("Save without a timeframe is stopped with a message", "timeframe" in page.inner_text("#hub-msg") and not page.is_visible("#dlg-hub-review"))
        # every number / choice box starts blank (timeframe shows the pair's own saved value)
        vals = page.eval_on_selector_all("#hub-panes input[type=number], #hub-panes select", "els=>els.filter(e=>e.id!=='hub_timeframe'&&e.id!=='hub-pair-select'&&e.id!=='hub_hold_level'&&e.id!=='hub_entry_wait'&&e.id!=='hub_trail_c_value').map(e=>e.value)")
        check("Candles Wait starts at 0 (owner 2026-10-25)", page.input_value("#hub_entry_wait") == "0")
        check("Hold rule ADX starts at 25 (owner 2026-10-25)", page.input_value("#hub_hold_level") == "25")
        check("Signal candle trailing Distance starts at 100 = 1 ATR, editable (owner 2026-10-28)", page.input_value("#hub_trail_c_value") == "100")
        check("every number/choice box starts blank (the pair's own timeframe is shown)", len(vals) > 20 and all(v == "" for v in vals), str([v for v in vals if v]))
        checked = page.eval_on_selector_all("#hub-panes input[type=checkbox]", "els=>els.filter(e=>e.checked&&e.id!=='hub_isolated'&&!/^hub_(stop_on|sl_on|tp_on|flip_same_entry|flip_same_hold|flip_same_wait|wfr|reentry)$/.test(e.id)).length")
        check("Fresh Reversal on First Trade and No Mid-Trend Re-entry start ticked (owner 2026-10-25)", page.is_checked("#hub_wfr") and page.is_checked("#hub_reentry"))
        check("every switch starts OFF (the picked optional functions switch their own box)", checked == 0, str(checked))
        check("the 3 flip Yes / No boxes start Yes (owner: Default Yes)", all(page.is_checked("#hub_" + x) for x in ("flip_same_entry", "flip_same_hold", "flip_same_wait")))
        openfn("flipact"); page.uncheck("#hub_flip_same_entry"); page.wait_for_timeout(100)   # this run sets its own FLIP rules
        # a short explanation beside every box
        for fn in ("pair", "chart", "ind", "combine", "flipact", "wait", "size", "maxloss", "tp"):
            openfn(fn)
            if fn == "ind":
                page.check("#hub_ma1_entry"); page.check("#hub_mx_entry"); page.check("#hub_adx_flip"); page.check("#hub_ma2_flip")
            if fn == "size":
                pick(page, "size_method", "Fixed base amount (grows with the last closed total)"); page.check("#hub_counter_on")
            if fn == "maxloss":
                page.select_option("#hub_stop_basis", "Fixed $ amount"); dismiss()
            if fn == "tp":
                page.check("#hub_tp1_on"); page.check("#hub_tp2_on"); page.check("#hub_tp3_on")
            rows = page.eval_on_selector_all(f"#hubpane_{fn} .hubx-row[data-fid]", "els=>els.filter(e=>e.offsetParent!==null).map(e=>[e.dataset.fid,(e.querySelector('.hubx-hint')||{}).innerText||''])")
            bad = [r[0] for r in rows if len(r[1].strip()) < 8]
            check(f"every visible box in {fn} has an explanation beside it ({len(rows)} boxes)", rows and not bad, str(bad))
        # reset what the explanation test switched on
        openfn("ind")
        for i in ("ma1_entry", "mx_entry", "adx_flip", "ma2_flip"): page.uncheck("#hub_" + i)
        openfn("size"); page.uncheck("#hub_counter_on"); pick(page, "size_method", "")
        openfn("maxloss"); page.select_option("#hub_stop_basis", ""); dismiss()
        openfn("tp")
        for i in (1, 2, 3): page.uncheck(f"#hub_tp{i}_on")
        # ---- Indicators
        openfn("ind")
        check("indicator list has all 11 with ENTRY and FLIP ticks", page.locator("#hubpane_ind .hubx-trow").count() == 12)
        check("indicator is named MA Crossover (old name gone)", "MA Crossover" in page.inner_text("#hubpane_ind .hubx-table") and "MA 1 vs MA 2" not in page.inner_text("#hubpane_ind"))
        check("no numbers shown for unticked indicators", not page.is_visible("#hubcard_rsi") and not page.is_visible("#hubcard_ma1"))
        page.select_option("#hub_chart_type", "Candles") if False else None
        openfn("chart"); page.select_option("#hub_chart_type", "Candles"); openfn("ind")
        check("Candle colour is greyed out on plain Candles", page.is_disabled("#hub_candle_entry") and page.is_disabled("#hub_candle_flip"))
        openfn("chart"); page.select_option("#hub_chart_type", "Heikin Ashi"); openfn("ind")
        check("Candle colour can be ticked on Heikin Ashi", not page.is_disabled("#hub_candle_entry"))
        page.check("#hub_candle_entry"); openfn("chart"); page.select_option("#hub_chart_type", "Candles"); openfn("ind")
        check("switching back to Candles unticks it", not page.is_checked("#hub_candle_entry"))
        openfn("chart"); page.select_option("#hub_chart_type", "Heikin Ashi"); openfn("ind")
        page.check("#hub_ma1_entry")
        check("ticking MA 1 shows its numbers", page.is_visible("#hubcard_ma1") and page.is_visible("#hub_ma1_length"))
        check("ALMA fields hidden until ALMA", not page.is_visible("#hub_ma1_alma_offset"))
        page.select_option("#hub_ma1_type", "ALMA"); check("ALMA fields show for ALMA", page.is_visible("#hub_ma1_alma_offset"))
        check("N hidden until a cross mode", not page.is_visible("#hub_ma1_n"))
        only(page, "ma1_modes", "Price crossed MA within N candles"); check("N shows for cross", page.is_visible("#hub_ma1_n"))
        page.select_option("#hub_ma1_type", "EMA"); only(page, "ma1_modes", "Price above / below MA")
        page.check("#hub_mx_flip")
        check("ticking MA Crossover also shows the MA 1 and MA 2 numbers", page.is_visible("#hubcard_ma2") and page.is_visible("#hub_ma2_length") and page.is_visible("#hub_mx_modes"))
        page.uncheck("#hub_mx_flip")
        check("MA 2 numbers hide again", not page.is_visible("#hubcard_ma2"))
        page.fill("#hub_ma1_length", "20")
        page.check("#hub_candle_entry"); page.check("#hub_candle_flip")
        # ---- Combine, On a flip signal
        openfn("combine")
        check("N boxes hidden until At least N", not page.is_visible("#hub_entry_min") and not page.is_visible("#hub_flip_min"))
        page.select_option("#hub_combine_entry", "At least N"); page.select_option("#hub_combine_flip", "At least N")
        check("N boxes show for At least N", page.is_visible("#hub_entry_min") and page.is_visible("#hub_flip_min"))
        page.fill("#hub_entry_min", "2"); page.fill("#hub_flip_min", "1")
        check("hold level hidden while the hold rule is not picked", not page.is_visible("#hub_hold_level"))
        openfn("flipact")
        # ---- Entry size, Entry wait
        openfn("size")
        check("lock-out box hidden for allocation / until a method is chosen", not page.is_visible("#hub_lock_pct"))
        pick(page, "size_method", "Fixed base amount (grows with the last closed total)")
        check("fixed: base amount and lock-out shown, allocation hidden", page.is_visible("#hub_base_amount") and page.is_visible("#hub_lock_pct") and not page.is_visible("#hub_alloc_pct"))
        pick(page, "size_method", "Allocation % of balance")
        check("allocation: lock-out hidden, allocation shown", not page.is_visible("#hub_lock_pct") and page.is_visible("#hub_alloc_pct"))
        pick(page, "size_method", "Fixed base amount (grows with the last closed total)")
        page.fill("#hub_base_amount", "100"); page.fill("#hub_lock_pct", "50"); page.fill("#hub_leverage", "10")
        page.check("#hub_margin_alert")
        # entry wait warning
        openfn("wait")
        page.fill("#hub_entry_wait", "0"); page.press("#hub_entry_wait", "Tab"); page.wait_for_timeout(150)
        check("no warning at 0 candles", not page.is_visible("#dlg-hub-wait"))
        page.fill("#hub_entry_wait", "3"); page.press("#hub_entry_wait", "Tab"); page.wait_for_timeout(200)
        check("warning pops up above 0", page.is_visible("#dlg-hub-wait") and "Delay in entry up to 3 candles" in page.inner_text("#dlg-hub-wait"), page.inner_text("#dlg-hub-wait") if page.is_visible("#dlg-hub-wait") else "")
        page.click("#hub-wait-ok"); page.wait_for_timeout(100)
        check("OK closes the warning", not page.is_visible("#dlg-hub-wait"))
        page.fill("#hub_entry_wait", "0"); page.press("#hub_entry_wait", "Tab")
        # ---- Max loss, Take profit
        openfn("maxloss")
        page.select_option("#hub_stop_basis", "% of entry amount"); dismiss(); page.fill("#hub_stop_pct", "5")
        check("max loss: % shown, $ hidden", page.is_visible("#hub_stop_pct") and not page.is_visible("#hub_stop_usd"))
        page.select_option("#hub_stop_basis", "Fixed $ amount"); dismiss()
        check("max loss $: $ shown, % hidden", page.is_visible("#hub_stop_usd") and not page.is_visible("#hub_stop_pct"))
        page.select_option("#hub_stop_basis", "% of entry amount"); dismiss()
        openfn("tp")
        check("TP ATR length hidden until % of ATR is chosen", not page.is_visible("#hub_tp_atr"))
        page.select_option("#hub_tp_basis", "% of ATR")
        page.fill("#hub_tp_atr", "14"); page.check("#hub_tp1_on"); page.fill("#hub_tp1_value", "150"); page.fill("#hub_tp1_pct", "50")
        check("TP 2 numbers hidden until TP 2 is used", not page.is_visible("#hub_tp2_value"))
        # ---- Stop Loss (a must)
        openfn("sl"); page.select_option("#hub_sl_basis", "% of price movement"); dismiss()
        page.fill("#hub_sl_value", "3"); page.press("#hub_sl_value", "Tab"); dismiss()
        # ---- Save
        page.fill("#hub_name", "BTC 1H compounding")
        page.select_option("#hub_timeframe", "4h") if False else None
        openfn("pair"); page.select_option("#hub_timeframe", "4h")
        check("Execute is on once every function is opened", not page.is_disabled("#hub-execute-btn"))
        page.click("#hub-save-btn"); page.wait_for_timeout(300)
        rv = page.inner_text("#hub-review-body")
        check("review lists the choices", "Strategy: BTC 1H compounding" in rv and "At least 2" in rv and "50% of the last profit" in rv and "Email alert on margin: ON" in rv and "5 % of entry amount" in rv, rv[:600])
        check("save review says Confirm and save", page.inner_text("#hub-review-apply") == "Confirm and save")
        page.click("#hub-review-apply"); page.wait_for_timeout(800)
        pc = store.get_account(acc.id).pairs["TESTUSDT"]
        pl = pc.plan or {}
        check("plan saved on the server", bool(pl), str(pl)[:300])
        check("name saved", pl.get("name") == "BTC 1H compounding")
        check("At least N saved with its numbers", pl.get("combine_entry") == "At least N" and pl.get("entry_min") == 2 and pl.get("combine_flip") == "At least N" and pl.get("flip_min") == 1, str((pl.get("combine_entry"), pl.get("entry_min"))))
        sz = pl.get("sizing", {})
        check("fixed base, lock-out and margin alert saved", sz.get("method") == "Fixed base amount (grows with the last closed total)" and sz.get("base_amount") == 100 and sz.get("lock_pct") == 50 and sz.get("margin_alert") is True and sz.get("leverage") == 10 and "alloc_pct" not in sz, str(sz))
        check("max loss saved with its basis", sz.get("stop_on") is True and sz.get("stop_basis") == "% of entry amount" and sz.get("stop_pct") == 5, str(sz))
        check("optional functions not picked are saved OFF", pl.get("trail", {}).get("on") is False and pl.get("shadow", {}).get("on") is False, str((pl.get("trail"), pl.get("shadow"))))
        check("take profit picked is saved ON with TP 1", pl.get("tp", {}).get("on") is True and pl["tp"]["levels"][0]["on"] is True and pl["tp"]["levels"][1]["on"] is False, str(pl.get("tp")))
        check("entry wait 0 kept (not dropped)", pl.get("entry_wait") == 0)
        check("timeframe saved with the pair", pc.timeframe == "4h", pc.timeframe)
        check("indicators saved", pl["ind"]["ma1"].get("entry") is True and pl["ind"]["ma1"].get("length") == 20 and pl["ind"]["candle"]["entry"] is True and pl["ind"]["rsi"].get("entry") is False, str(pl.get("ind", {}).get("ma1")))
        check("success message shown, nothing started by Save", "Plan saved" in page.inner_text("#hub-msg") and not STARTED, page.inner_text("#hub-msg"))
        # ---- Execute
        page.click("#hub-execute-btn"); page.wait_for_timeout(300)
        check("Execute review says START trading", "START trading" in page.inner_text("#hub-review-apply") and "start" in page.inner_text("#hub-review-title").lower())
        page.click("#hub-review-apply"); page.wait_for_timeout(800)
        check("Execute saved and started the pair", len(STARTED) == 1 and "started" in page.inner_text("#hub-msg"), page.inner_text("#hub-msg"))
        # ---- the plan is saved but the start is refused (e.g. live trading switch OFF): say so plainly
        START_FAILS.append(1)
        page.click("#hub-execute-btn"); page.wait_for_timeout(300); page.click("#hub-review-apply"); page.wait_for_timeout(700)
        msg = page.inner_text("#hub-msg")
        check("start refused: message says the plan is saved but the pair did not start, with the reason", msg.startswith("Plan saved, but the pair did not start") and "live trading is OFF" in msg and len(STARTED) == 1, msg[:200])
        # ---- a refused plan shows the server's words and starts nothing
        openfn("combine"); page.fill("#hub_entry_min", "9")
        page.click("#hub-execute-btn"); page.wait_for_timeout(300); page.click("#hub-review-apply"); page.wait_for_timeout(700)
        msg = page.inner_text("#hub-msg")
        check("N bigger than the ticked indicators is refused with the server's words, nothing started", msg.startswith("Not saved") and "N is 9" in msg and len(STARTED) == 1, msg[:200])
        check("the saved plan is unchanged after the refusal", (store.get_account(acc.id).pairs["TESTUSDT"].plan or {}).get("entry_min") == 2)
        # ---- reload: the saved plan comes back, with its picks
        page.reload(); page.wait_for_timeout(600); page.click("#maintab-hub")
        choose_account_and_pair()
        page.click("#hub-confirm-btn") if page.is_visible("#hub-confirm-btn") else None
        page.wait_for_timeout(100)
        page.select_option("#hub-pair-select", index=1) if page.is_visible("#hub-pair-select") else (openfn("pair"), page.select_option("#hub-pair-select", index=1))
        page.wait_for_timeout(700)
        check("saved plan skips the pick step and shows the fill step", page.is_visible("#hub-fill-pane") and not page.is_visible("#hub-pick-pane"))
        check("name loads back", page.input_value("#hub_name") == "BTC 1H compounding")
        check("state line shows the saved plan", "Saved plan" in page.inner_text("#hub-state"))
        picked = page.eval_on_selector_all('.hubx-item[aria-pressed="true"]', "els=>els.map(e=>e.dataset.fn)")
        check("picks come back from the plan (max loss + Stop Loss + take profit yes, trailing + shadow no)", picked == ["pair", "chart", "ind", "combine", "flipact", "wait", "size", "maxloss", "sl", "tp"], str(picked))
        check("Execute is on for a saved plan", not page.is_disabled("#hub-execute-btn"))
        openfn("size")
        check("fixed base, lock-out and margin alert load back", page.input_value("#hub_base_amount") == "100" and page.input_value("#hub_lock_pct") == "50" and page.is_checked("#hub_margin_alert") and page.input_value("#hub_leverage") == "10")
        openfn("combine")
        check("At least N loads back", page.input_value("#hub_combine_entry") == "At least N" and page.input_value("#hub_entry_min") == "2")
        openfn("tp")
        check("take profit loads back", page.input_value("#hub_tp_basis") == "% of ATR" and page.input_value("#hub_tp1_value") == "150" and page.input_value("#hub_tp1_pct") == "50")
        # ---- changing the account asks for the pair again (nothing saved to the wrong place)
        page.select_option("#hub-acct-select", index=2); page.wait_for_timeout(200)
        check("a different account clears the pair choice", page.input_value("#hub-pair-select") == "")
        page.click("#hub-save-btn"); page.wait_for_timeout(200)
        check("Save with no pair chosen is stopped", "pair" in page.inner_text("#hub-msg").lower() and not page.is_visible("#dlg-hub-review"), page.inner_text("#hub-msg"))
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(200)
        # ---- layouts
        page.set_viewport_size({"width": 800, "height": 900}); page.wait_for_timeout(200)
        check("tablet: menu above, no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"), str(page.evaluate("document.documentElement.scrollWidth")))
        page.set_viewport_size({"width": 1500, "height": 900}); page.wait_for_timeout(200)
        check("desktop: menu on the left, settings on the right", page.evaluate("document.getElementById('hub-menu').getBoundingClientRect().right<=document.getElementById('hub-panes').getBoundingClientRect().left+2"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
