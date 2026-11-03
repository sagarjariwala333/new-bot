"""
browser_presets_check.py - OPTIONAL real-browser check of the PRESETS (2026-10-28): the Presets button on the Strategy Hub,
Load of "HMA Flip", "MACD Cross", "V3", "RSI MA Cross" and "EMA Cross" (rule choices only, NO NUMBERS), nothing left over from the other preset,
a preset cannot be changed, and a preset with arbitrary numbers keyed in is accepted by the real library route
(Playwright + Chromium). Backend = the REAL plan and library routes of app/api; no exchange calls.
Run:  python tests/browser_presets_check.py     (arbitrary numbers only, NOT the owner's settings)
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_presets")
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
        m = re.fullmatch(rf"/api/accounts/{acc.id}/pairs/([A-Z]+)(/plan|/start)?", path)
        if m:
            sym, tail = m.group(1), m.group(2) or ""
            if tail == "/plan" and method == "GET": return 200, asyncio.run(api.get_pair_plan(acc.id, sym))
            if tail == "/plan" and method == "PUT": return 200, asyncio.run(api.set_pair_plan(acc.id, sym, Body(payload)))
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


FIXED = "Fixed base amount (grows with the last closed total)"
ALLOC = "Allocation % of balance"
STARTS = ["hub_entry_wait", "hub_hold_level", "hub_trail_c_value"]    # the form's own starting boxes (owner-stated)


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def wait(ms=200): page.wait_for_timeout(ms)
    def val(i): return page.input_value(f"#hub_{i}")
    def on(i): return page.evaluate("id => document.getElementById('hub_' + id).checked", i)
    def ticks(i): return page.evaluate("id => [...document.querySelectorAll('#hub_' + id + ' input')].filter(x => x.checked).map(x => x.value)", i)
    def picked(): return set(page.evaluate("[...document.querySelectorAll('.hubx-item[aria-pressed=true]')].map(e => e.dataset.fn)"))
    def numbers_filled(): return sorted(page.evaluate("[...document.querySelectorAll('#hub-panes input[type=number]')].filter(e => e.value !== '').map(e => e.id)"))
    def setv(i, v):
        page.evaluate("([i, v]) => { const e = document.getElementById('hub_' + i); e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); }", [i, str(v)])
    def tick(sel):
        page.evaluate("s => { const e = document.querySelector(s); e.checked = true; e.dispatchEvent(new Event('input', {bubbles: true})); }", sel)
    def load_preset(pid):
        page.click("#hub-preset-open-btn"); wait(250)
        page.click(f'#dlg-presets .lib-row[data-id="{pid}"] .preset-load'); wait(250)
        page.click("#lib-confirm-ok"); wait(300)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; warns = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: warns.append(m.text) if "No help text" in m.text else None)
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        check("Presets button is on the Strategy Hub, with an i", page.is_visible("#hub-preset-open-btn")
              and page.evaluate("document.getElementById('hub-preset-open-btn').nextElementSibling.classList.contains('info-i')"))
        check("its help text has the 6 parts", page.evaluate("HELP['preset.open'].length") == 6)
        page.select_option("#hub-acct-select", index=1); wait(300)
        before = page.evaluate("JSON.stringify(HUB_PRESETS)")

        # ---- the list; Load before a pair is chosen
        page.click("#hub-preset-open-btn"); wait(250)
        txt = page.inner_text("#preset-list")
        check("the list shows all five presets", all(x in txt for x in ("HMA Flip", "MACD Cross", "V3", "RSI MA Cross", "EMA Cross")), txt[:200])
        check("each has Details and a Load button, nothing to delete or edit",
              page.locator("#dlg-presets .preset-load").count() == 5 and page.locator("#dlg-presets details").count() == 5
              and page.locator("#dlg-presets .lib-del").count() == 0 and page.locator("#dlg-presets input").count() == 0)
        page.click('#dlg-presets .lib-row[data-id="hma_flip"] .preset-load'); wait(200)
        check("Load before a pair is chosen is refused with a message",
              "pair" in page.inner_text("#preset-msg") and not page.is_visible("#dlg-lib-confirm"), page.inner_text("#preset-msg"))
        page.click("#dlg-presets button.ghost:has-text('Close')"); wait()

        # ---- HMA Flip onto AAAUSDT (something typed first must be replaced)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        page.evaluate("document.getElementById('hub_name').value = 'typed before'")
        setv("base_amount", "999")
        load_preset("hma_flip")
        check("HMA Flip: loaded message, name set, pair timeframe untouched",
              "HMA Flip" in page.inner_text("#hub-msg") and val("timeframe") == "4h", page.inner_text("#hub-msg"))
        check("name is HMA Flip (the typed one is replaced)", page.input_value("#hub_name") == "HMA Flip")
        check("Chart type: Candles", val("chart_type") == "Candles")
        check("MA 1: HMA, MA rising / falling, ENTRY ticked, FLIP not", val("ma1_type") == "HMA"
              and ticks("ma1_modes") == ["MA rising / falling"] and on("ma1_entry") and not on("ma1_flip"))
        check("MA 2: EMA, CTREND ticked, not ticked for ENTRY or FLIP", val("ma2_type") == "EMA" and on("ma2_ctrend")
              and not on("ma2_entry") and not on("ma2_flip") and on("counter_on") and val("counter_by") == "ma2")
        check("no ADX filter (ADX not ticked for ENTRY or FLIP)", not on("adx_entry") and not on("adx_flip"))
        check("On a flip signal: all 3 Yes", on("flip_same_entry") and on("flip_same_hold") and on("flip_same_wait"))
        check("Fresh Reversal ticked, No Mid-Trend Re-entry ticked", on("wfr") and on("reentry"))
        check("Entry size: Fixed base amount", page.evaluate("[...document.querySelectorAll('#hub_size_method input:checked')].map(x => x.value)") == [FIXED])
        check("Break-Even After ATR (not After TP)", on("be_after_atr") and not on("be_after_tp"))
        check("Take profit: % of ATR, from the signal candle close", val("tp_basis") == "% of ATR" and val("tp_from") == "Signal candle close")
        check("menu: Hold rule, Max Loss, SL, BE, TS, TP picked; Shadow not",
              picked() == {"pair", "chart", "ind", "combine", "flipact", "hold", "wait", "size", "maxloss", "sl", "be", "trail", "tp"}, str(sorted(picked())))
        check("TS type is Signal candle high / low +/- ATR, ATR length empty",
              val("trail_type") == "Signal candle high / low +/- ATR" and val("trail_c_atr") == "")
        check("NOT set (left for the owner): Indicator Combination, Max Loss basis, SL basis, timeframe pick",
              val("combine_entry") == "" and val("stop_basis") == "" and val("sl_basis") == "")
        check("NO NUMBERS: the typed 999 is gone and every number box is empty except the 3 starting boxes",
              numbers_filled() == STARTS, str(numbers_filled()))
        check("the starting boxes are the form's own: Candles Wait 0, Hold rule ADX 25, TS distance 100",
              val("entry_wait") == "0" and val("hold_level") == "25" and val("trail_c_value") == "100")
        check("Execute stays off until every function is opened", page.is_disabled("#hub-execute-btn"))
        # the owner keys in his own numbers and the missing choices (arbitrary ones here), then Save to library
        setv("combine_entry", "ALL"); setv("ma1_length", 9); setv("ma2_length", 40)
        tick('#hub_ma2_modes input[value="Price above / below MA"]')
        setv("adx_di_length", 7); setv("adx_smoothing", 7); setv("adx_level", 15)
        setv("base_amount", 50); setv("leverage", 3); setv("lock_pct", 40); setv("counter_base", 20); setv("counter_lev", 2)
        setv("stop_basis", "% of entry amount"); setv("stop_pct", 5)
        setv("sl_basis", "% of ATR"); setv("sl_value", 150); setv("sl_atr", 9)
        setv("be_atr_value", 100); setv("be_atr_length", 9)
        setv("trail_c_atr", 9)
        setv("tp_atr", 9); tick("#hub_tp1_on"); setv("tp1_value", 100); setv("tp1_pct", 50)
        page.click("#hub-lib-save-btn"); wait(250)
        check("Save to library is offered and the name is HMA Flip", page.input_value("#lib-save-name") == "HMA Flip")
        page.click("#lib-save-ok"); wait(600)
        saved = L.list_all()["saved"]
        check("with the owner's numbers keyed in, the real library route accepts it", len(saved) == 1 and saved[0]["name"] == "HMA Flip",
              page.inner_text("#lib-save-msg") + page.inner_text("#hub-msg"))
        sp = saved[0]["plan"] if saved else {}
        check("the saved plan holds the preset's choices",
              sp.get("chart_type") == "Candles" and sp["ind"]["ma1"]["type"] == "HMA" and sp["ind"]["ma1"]["entry"] is True
              and sp["tp"]["from"] == "Signal candle close" and sp["allow_reentry"] is False and sp["wait_first_reversal"] is True
              and sp["sizing"]["method"] == FIXED and sp["sizing"]["counter"]["by"] == "ma2" and sp["be"]["after_atr"] is True,
              str(sp)[:300])

        # ---- MACD Cross onto BBBUSDT: nothing left over from HMA Flip
        page.select_option("#hub-pair-select", "BBBUSDT"); wait(500)
        load_preset("macd_cross")
        check("MACD Cross: loaded, name set, this pair's timeframe untouched", "MACD Cross" in page.inner_text("#hub-msg")
              and page.input_value("#hub_name") == "MACD Cross" and val("timeframe") == "1h")
        check("MACD: ENTRY and FLIP ticked, Histogram above / below 0 (NOT Zero line)",
              on("macd_entry") and on("macd_flip") and ticks("macd_modes") == ["Histogram above / below 0"] and not on("macd_rising"))
        check("MACD Fast, Slow, Signal are empty", val("macd_fast") == "" and val("macd_slow") == "" and val("macd_signal") == "")
        check("Chart type is NOT set", val("chart_type") == "")
        check("Entry size: Fixed base amount, amount empty", page.evaluate("[...document.querySelectorAll('#hub_size_method input:checked')].map(x => x.value)") == [FIXED]
              and val("base_amount") == "" and val("leverage") == "")
        check("Trailing Stop: Signal candle high / low +/- ATR; distance starts at its own start, ATR length empty",
              val("trail_type") == "Signal candle high / low +/- ATR" and val("trail_c_value") == "100" and val("trail_c_atr") == "")
        check("Take profit is not picked", "tp" not in picked())
        check("menu: only Stop Loss, Trailing Stop and the form's own Hold rule are picked besides the core ones",
              picked() == {"pair", "chart", "ind", "combine", "flipact", "hold", "wait", "size", "sl", "trail"}, str(sorted(picked())))
        check("nothing left over from HMA Flip",
              not on("ma1_entry") and val("ma1_type") == "" and val("ma1_length") == "" and not on("ma2_ctrend") and not on("counter_on")
              and not on("be_after_atr") and val("tp_from") == "" and val("tp_basis") == "" and val("stop_basis") == ""
              and val("sl_basis") == "" and val("combine_entry") == "" and not on("tp1_on") and ticks("ma1_modes") == [])
        check("left as a new form starts: No Mid-Trend Re-entry ticked, Fresh Reversal ticked, flip boxes Yes",
              on("reentry") and on("wfr") and on("flip_same_entry") and on("flip_same_hold") and on("flip_same_wait"))
        check("NO NUMBERS: every number box is empty except the 3 starting boxes", numbers_filled() == STARTS, str(numbers_filled()))
        setv("chart_type", "Candles"); setv("combine_entry", "ALL")
        setv("macd_fast", 5); setv("macd_slow", 13); setv("macd_signal", 4)
        setv("base_amount", 50); setv("leverage", 3); setv("lock_pct", 40)
        setv("adx_di_length", 7); setv("adx_smoothing", 7); setv("adx_level", 15)
        setv("sl_basis", "% of price movement"); setv("sl_value", 3); setv("trail_c_atr", 9)
        page.click("#hub-lib-save-btn"); wait(250)
        page.fill("#lib-save-name", "MACD Cross copy"); page.click("#lib-save-ok"); wait(600)
        saved = L.list_all()["saved"]
        check("with the owner's numbers keyed in, the real library route accepts it", len(saved) == 2 and saved[0]["name"] == "MACD Cross copy",
              page.inner_text("#lib-save-msg") + page.inner_text("#hub-msg"))
        sp = saved[0]["plan"] if saved else {}
        check("the saved plan holds the preset's choices",
              sp.get("ind", {}).get("macd", {}).get("modes") == ["Histogram above / below 0"] and sp["trail"]["type"] == "Signal candle high / low +/- ATR"
              and sp["tp"]["on"] is False and sp["sl"]["on"] is True and sp["sizing"]["method"] == FIXED, str(sp)[:300])

        # ---- V3 onto AAAUSDT: nothing left over from MACD Cross / HMA Flip (owner 2026-11-02)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        load_preset("v3")
        check("V3: loaded, name set, this pair's timeframe untouched", "V3" in page.inner_text("#hub-msg")
              and page.input_value("#hub_name") == "V3" and val("timeframe") == "4h", page.inner_text("#hub-msg"))
        check("V3: Chart type Heikin Ashi, Indicator Combination ALL", val("chart_type") == "Heikin Ashi" and val("combine_entry") == "ALL")
        check("V3: Candle colour ticked for ENTRY only", on("candle_entry") and not on("candle_flip") and not on("candle_ctrend"))
        check("V3: MA 1 = HMA, MA rising / falling, ENTRY", val("ma1_type") == "HMA" and ticks("ma1_modes") == ["MA rising / falling"]
              and on("ma1_entry") and not on("ma1_flip") and not on("ma1_ctrend"))
        check("V3: MA 2 = EMA, Price above / below MA, ENTRY (not CTREND, not FLIP)", val("ma2_type") == "EMA"
              and ticks("ma2_modes") == ["Price above / below MA"] and on("ma2_entry") and not on("ma2_flip") and not on("ma2_ctrend"))
        check("V3: no ADX / MACD / RSI ticked for ENTRY or FLIP", not on("adx_entry") and not on("adx_flip") and not on("macd_entry")
              and not on("macd_flip") and not on("rsi_entry") and not on("rsi_flip"))
        check("V3: all 3 flip answers Yes, Fresh Reversal ticked, No Mid-Trend Re-entry ticked",
              on("flip_same_entry") and on("flip_same_hold") and on("flip_same_wait") and on("wfr") and on("reentry"))
        check("V3: Entry size = Allocation % of balance", page.evaluate("[...document.querySelectorAll('#hub_size_method input:checked')].map(x => x.value)") == [ALLOC])
        check("V3: Max Loss ticked, % of available balance; Counter-trend ticked, decided by EMA length",
              on("stop_on") and val("stop_basis") == "% of available balance" and on("counter_on") and val("counter_by") == "EMA")
        check("V3: Shadow ticked; Break-Even, Trailing Stop, Take profit NOT ticked; Stop Loss has no 'Measured on' (= no SL)",
              on("shadow_on") and not on("be_on") and not on("trail_on") and not on("tp_on") and val("sl_basis") == "")
        check("V3 menu: core (Stop Loss is always in the menu) + Hold rule + Max Loss + Shadow picked, no BE / TS / TP",
              picked() == {"pair", "chart", "ind", "combine", "flipact", "hold", "wait", "size", "sl", "maxloss", "shadow"}, str(sorted(picked())))
        check("V3: nothing left over from MACD Cross or HMA Flip", not on("macd_entry") and not on("macd_flip") and ticks("macd_modes") == []
              and val("sl_basis") == "" and val("trail_type") == "" and val("tp_basis") == "")
        check("V3 NO NUMBERS: every number box is empty except the 3 starting boxes", numbers_filled() == STARTS, str(numbers_filled()))
        check("V3: Execute stays off until every function is opened", page.is_disabled("#hub-execute-btn"))
        # the owner keys in his own numbers (arbitrary ones here), then Save to library
        setv("ma1_length", 9); setv("ma2_length", 40)
        setv("adx_di_length", 7); setv("adx_smoothing", 7); setv("adx_level", 15)
        setv("alloc_pct", 20); setv("leverage", 5); setv("counter_alloc", 10); setv("counter_lev", 3); setv("counter_ema", 100)
        setv("stop_pct", 6)
        setv("shadow_trades", 3); setv("shadow_slip", 1); setv("shadow_start", 100)
        page.click("#hub-lib-save-btn"); wait(250)
        check("V3: Save to library is offered and the name is V3", page.input_value("#lib-save-name") == "V3")
        page.click("#lib-save-ok"); wait(600)
        saved = L.list_all()["saved"]
        check("V3 with the owner's numbers keyed in, the real library route accepts it", len(saved) == 3 and saved[0]["name"] == "V3",
              page.inner_text("#lib-save-msg") + page.inner_text("#hub-msg"))
        sp = saved[0]["plan"] if saved else {}
        check("V3: the saved plan holds the preset's choices",
              sp.get("chart_type") == "Heikin Ashi" and sp["combine_entry"] == "ALL" and sp["ind"]["candle"]["entry"] is True
              and sp["ind"]["ma1"]["type"] == "HMA" and sp["ind"]["ma2"]["type"] == "EMA"
              and sp["allow_reentry"] is False and sp["wait_first_reversal"] is True and sp["hold"]["on"] is True
              and sp["sizing"]["method"] == ALLOC and sp["sizing"]["stop_on"] is True and sp["sizing"]["counter"]["by"] == "EMA"
              and sp["shadow"]["on"] is True and sp["sl"]["on"] is False and sp["tp"]["on"] is False
              and sp["be"]["on"] is False and sp["trail"]["on"] is False, str(sp)[:300])

        # ---- RSI MA Cross onto BBBUSDT: nothing left over from V3 (owner 2026-11-02)
        page.select_option("#hub-pair-select", "BBBUSDT"); wait(500)
        load_preset("rsi_ma_cross")
        check("RSI MA Cross: loaded, name set, this pair's timeframe untouched", "RSI MA Cross" in page.inner_text("#hub-msg")
              and page.input_value("#hub_name") == "RSI MA Cross" and val("timeframe") == "1h", page.inner_text("#hub-msg"))
        check("RSI MA Cross: Chart type Candles, Indicator Combination ALL", val("chart_type") == "Candles" and val("combine_entry") == "ALL")
        check("RSI: ENTRY ticked, ONLY 'RSI crosses its own MA' (no level, no OB/OS, no divergence)",
              on("rsi_entry") and not on("rsi_flip") and ticks("rsi_modes") == ["RSI crosses its own MA"], str(ticks("rsi_modes")))
        check("MA 1 = HMA, MA rising / falling, ENTRY; ADX ENTRY ticked",
              val("ma1_type") == "HMA" and ticks("ma1_modes") == ["MA rising / falling"] and on("ma1_entry") and on("adx_entry")
              and not on("adx_flip") and not on("ma1_flip"))
        check("no MA 2, MACD, Candle colour ticked", not on("ma2_entry") and not on("macd_entry") and not on("candle_entry"))
        check("all 3 flip answers Yes, Fresh Reversal ticked, No Mid-Trend Re-entry ticked",
              on("flip_same_entry") and on("flip_same_hold") and on("flip_same_wait") and on("wfr") and on("reentry"))
        check("Entry size = Fixed base amount; Counter-trend ticked, decided by MA 1",
              page.evaluate("[...document.querySelectorAll('#hub_size_method input:checked')].map(x => x.value)") == [FIXED]
              and on("counter_on") and val("counter_by") == "ma1")
        check("Max Loss, Stop Loss, Trailing Stop, Shadow, Cooldown ticked; Break-Even, Take profit, Loss limit NOT",
              on("stop_on") and on("sl_on") and on("trail_on") and on("shadow_on") and on("cooldown_on")
              and not on("be_on") and not on("tp_on") and not on("loss_on"))
        check("Trailing Stop type, Max Loss basis, Stop Loss basis are left empty for the owner",
              val("trail_type") == "" and val("stop_basis") == "" and val("sl_basis") == "")
        check("menu: core + Hold rule, Max Loss, Trailing Stop, Shadow, Cooldown picked; no BE / TP / Loss limit",
              picked() == {"pair", "chart", "ind", "combine", "flipact", "hold", "wait", "size", "sl", "maxloss", "trail", "shadow", "cooldown"}, str(sorted(picked())))
        check("nothing left over from V3", val("chart_type") == "Candles" and not on("candle_entry") and val("ma2_type") == "" and val("combine_entry") == "ALL")
        check("NO NUMBERS: every number box is empty except the 3 starting boxes (incl. RSI length, MA length, N, lock %)",
              numbers_filled() == STARTS, str(numbers_filled()))
        check("Execute stays off until every function is opened", page.is_disabled("#hub-execute-btn"))
        # the owner keys in his own numbers (arbitrary ones here), then Save to library
        setv("rsi_length", 14); setv("rsi_ma_type", "SMA"); setv("rsi_ma_length", 9); setv("rsi_n", 3)
        setv("ma1_length", 9)
        setv("adx_di_length", 7); setv("adx_smoothing", 7); setv("adx_level", 15)
        setv("base_amount", 50); setv("leverage", 3); setv("lock_pct", 40); setv("counter_base", 20); setv("counter_lev", 2)
        setv("stop_basis", "% of entry amount"); setv("stop_pct", 5)
        setv("sl_basis", "% of price movement"); setv("sl_value", 3)
        setv("trail_type", "Signal candle high / low +/- ATR"); setv("trail_c_atr", 9)
        setv("shadow_trades", 3); setv("shadow_slip", 1); setv("shadow_start", 100)
        setv("cd_candles", 4)
        page.click("#hub-lib-save-btn"); wait(250)
        check("RSI MA Cross: Save to library is offered and the name is RSI MA Cross", page.input_value("#lib-save-name") == "RSI MA Cross")
        page.click("#lib-save-ok"); wait(600)
        saved = L.list_all()["saved"]
        check("RSI MA Cross with the owner's numbers keyed in, the real library route accepts it",
              len(saved) == 4 and saved[0]["name"] == "RSI MA Cross", page.inner_text("#lib-save-msg") + page.inner_text("#hub-msg"))
        sp = saved[0]["plan"] if saved else {}
        check("RSI MA Cross: the saved plan holds the preset's choices",
              sp.get("chart_type") == "Candles" and sp["combine_entry"] == "ALL" and sp["ind"]["rsi"]["modes"] == ["RSI crosses its own MA"]
              and sp["ind"]["rsi"]["entry"] is True and sp["ind"]["ma1"]["type"] == "HMA" and sp["ind"]["adx"]["entry"] is True
              and sp["allow_reentry"] is False and sp["wait_first_reversal"] is True and sp["hold"]["on"] is True
              and sp["sizing"]["method"] == FIXED and sp["sizing"]["lock_pct"] == 40 and sp["sizing"]["counter"]["by"] == "ma1"
              and sp["shadow"]["on"] is True and sp["cooldown"]["on"] is True and sp["be"]["on"] is False and sp["tp"]["on"] is False
              and sp["trail"]["on"] is True, str(sp)[:300])

        # ---- EMA Cross onto AAAUSDT: nothing left over from the RSI MA Cross (owner 2026-11-03)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        load_preset("ema_cross")
        check("EMA Cross: loaded, name set, this pair's timeframe untouched", "EMA Cross" in page.inner_text("#hub-msg")
              and page.input_value("#hub_name") == "EMA Cross" and val("timeframe") == "4h", page.inner_text("#hub-msg"))
        check("EMA Cross: Chart type Candles", val("chart_type") == "Candles")
        check("EMA Cross: MA Crossover ticked for ENTRY, only 'MA 1 crossed MA 2 within N candles'",
              on("mx_entry") and not on("mx_flip") and ticks("mx_modes") == ["MA 1 crossed MA 2 within N candles"], str(ticks("mx_modes")))
        check("EMA Cross: MA 1 = EMA and MA 2 = EMA, not ticked for ENTRY / FLIP / CTREND, no condition ticked (owner's pick)",
              val("ma1_type") == "EMA" and val("ma2_type") == "EMA" and not on("ma1_entry") and not on("ma2_entry")
              and not on("ma1_flip") and not on("ma2_flip") and not on("ma1_ctrend") and not on("ma2_ctrend")
              and ticks("ma1_modes") == [] and ticks("ma2_modes") == [])
        check("EMA Cross: ADX and RSI are there but NOT ticked", not on("adx_entry") and not on("adx_flip") and not on("rsi_entry") and not on("rsi_flip"))
        check("EMA Cross: no MACD / Candle colour ticked", not on("macd_entry") and not on("candle_entry"))
        check("EMA Cross: all 3 flip answers Yes", on("flip_same_entry") and on("flip_same_hold") and on("flip_same_wait"))
        check("EMA Cross: Hold rule NOT picked", not on("hold_on"))
        check("EMA Cross: Entry size = Fixed base amount, counter-trend not ticked",
              page.evaluate("[...document.querySelectorAll('#hub_size_method input:checked')].map(x => x.value)") == [FIXED] and not on("counter_on"))
        check("EMA Cross: Trailing Stop ticked, type Signal candle high / low +/- ATR; Break-Even, Take profit NOT ticked",
              on("trail_on") and val("trail_type") == "Signal candle high / low +/- ATR" and not on("be_on") and not on("tp_on"))
        check("EMA Cross: left for the owner - Max Loss basis, Stop Loss basis, Shadow, Cooldown, Loss limit, Combination",
              val("stop_basis") == "" and val("sl_basis") == "" and not on("shadow_on") and not on("cooldown_on") and not on("loss_on")
              and val("combine_entry") == "")
        check("EMA Cross menu: core + Trailing Stop picked; no Hold rule, Max Loss, BE, TP, Shadow, Cooldown, Loss limit",
              picked() == {"pair", "chart", "ind", "combine", "flipact", "wait", "size", "sl", "trail"}, str(sorted(picked())))
        check("EMA Cross: nothing left over from RSI MA Cross", not on("rsi_entry") and ticks("rsi_modes") == [] and not on("cooldown_on")
              and not on("counter_on") and val("rsi_length") == "" and val("ma1_length") == "")
        check("EMA Cross NO NUMBERS: every number box is empty except the form's own starting boxes", numbers_filled() == STARTS, str(numbers_filled()))
        check("EMA Cross: the Trailing Stop distance starts at the form's 100 (the owner changes it)", val("trail_c_value") == "100")
        check("EMA Cross: Execute stays off until every function is opened", page.is_disabled("#hub-execute-btn"))
        # the owner keys in his numbers and picks the two conditions the Hub asks for (arbitrary ones here)
        tick('#hub_ma1_modes input[value="MA rising / falling"]'); tick('#hub_ma2_modes input[value="MA rising / falling"]')
        setv("combine_entry", "ALL")
        setv("ma1_length", 9); setv("ma2_length", 21); setv("mx_n", 0)
        setv("base_amount", 50); setv("leverage", 3); setv("lock_pct", 50)
        setv("trail_c_atr", 14); setv("trail_c_value", 0.0001)
        page.click("#hub-lib-save-btn"); wait(250)
        check("EMA Cross: Save to library is offered and the name is EMA Cross", page.input_value("#lib-save-name") == "EMA Cross")
        page.click("#lib-save-ok"); wait(600)
        saved = L.list_all()["saved"]
        check("EMA Cross with the owner's numbers keyed in, the real library route accepts it",
              len(saved) == 5 and saved[0]["name"] == "EMA Cross", page.inner_text("#lib-save-msg") + page.inner_text("#hub-msg"))
        sp = saved[0]["plan"] if saved else {}
        check("EMA Cross: the saved plan holds the preset's choices",
              sp.get("chart_type") == "Candles" and sp["ind"]["mx"]["entry"] is True and sp["ind"]["mx"]["modes"] == ["MA 1 crossed MA 2 within N candles"]
              and sp["ind"]["mx"]["n"] == 0 and sp["ind"]["ma1"]["type"] == "EMA" and sp["ind"]["ma2"]["type"] == "EMA"
              and sp["ind"]["ma1"]["length"] == 9 and sp["ind"]["ma2"]["length"] == 21 and sp["hold"]["on"] is False
              and sp["sizing"]["method"] == FIXED and sp["sizing"]["lock_pct"] == 50
              and sp["trail"]["on"] is True and sp["trail"]["type"] == "Signal candle high / low +/- ATR" and sp["trail"]["value"] == 0.0001
              and sp["be"]["on"] is False and sp["tp"]["on"] is False, str(sp)[:300])
        # ADX / RSI can be ticked after Load (owner: "I may want to use next time")
        tick("#hub_adx_entry"); tick("#hub_rsi_entry")
        check("EMA Cross: ADX and RSI can be ticked on top after Load", on("adx_entry") and on("rsi_entry"))

        # ---- a preset cannot be changed
        page.evaluate("try { HUB_PRESETS[0].plan.chart_type = 'Renko'; HUB_PRESETS.push({}); } catch (e) {}")
        check("a preset is read only (frozen) and loading did not change it",
              page.evaluate("Object.isFrozen(HUB_PRESETS) && Object.isFrozen(HUB_PRESETS[0].plan)") and page.evaluate("JSON.stringify(HUB_PRESETS)") == before)
        nums = page.evaluate("""() => { const out = []; const walk = (o, p) => { if (o && typeof o === 'object') Object.entries(o).forEach(([k, v]) => walk(v, p + '.' + k)); else if (typeof o === 'number') out.push(p); };
                                  walk(HUB_PRESETS.map(x => x.plan), 'plan'); return out; }""")
        check("the live preset data has no number anywhere", nums == [], str(nums))
        check("no page errors", not errors, str(errors[:3]))
        check("no 'No help text' warning", not warns, str(warns[:3]))
        b.close()
    server.shutdown()
    ok = sum(res)
    print(f"\n{ok}/{len(res)} checks passed")
    sys.exit(0 if ok == len(res) else 1)


if __name__ == "__main__":
    main()
