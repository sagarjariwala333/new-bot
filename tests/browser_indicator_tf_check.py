"""
browser_indicator_tf_check.py - OPTIONAL real-browser check of the Timeframe box on every indicator and of MFI / CCI / Stochastic / CMO / OBV as Entry / Flip indicators (2026-11-18)
in the Hub Indicators tab. Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_indicator_tf_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_indicator_tf")
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
PLAN1 = make_plan(ind_rsi={"entry": True}, ind_macd={"entry": True}, name="Indicator timeframe test")
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
        page.click('.hubx-item[data-fn="ind"]'); wait(150)
    def blk(k): return ((plan_of("AAAUSDT") or {}).get("ind") or {}).get(k, {})
    TFS = ["Chart", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "2d", "3d", "5d", "1w", "1M"]
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        warns = []; page.on("console", lambda m: warns.append(m.text) if "No help text" in m.text else None)
        page.goto(base); wait(600)
        open_pair()
        rows = page.eval_on_selector_all("#hubpane_ind .hubx-trow:not(.hubx-thead) > span:first-child", "xs => xs.map(x => x.firstChild.textContent.trim())")
        check("indicator table has 21 rows incl. MFI, CCI, Stochastic, CMO, OBV", len(rows) == 21 and all(n in rows for n in ("MFI", "CCI", "Stochastic", "CMO", "OBV")), str(rows))
        order = [r for r in rows if r in ("RSI", "MFI", "CCI", "Stochastic", "CMO", "OBV", "MACD")]
        check("the new ones sit after RSI, before MACD", order == ["RSI", "MFI", "CCI", "Stochastic", "CMO", "OBV", "MACD"], str(order))
        for k in ("mfi", "cci", "stoch", "cmo", "obv"):
            check(f"{k}: ENTRY, FLIP and CTREND boxes exist and are unticked",
                  page.query_selector(f"#hub_{k}_entry") and not page.is_checked(f"#hub_{k}_entry") and not page.is_checked(f"#hub_{k}_flip")
                  and page.query_selector(f"#hub_{k}_ctrend") is not None)
        # timeframe box
        tf_ids = page.eval_on_selector_all("#hubpane_ind select[id$='_tf']", "xs => xs.map(x => x.id)")
        want = ["hub_" + k + "_tf" for k in ("candle", "ma1", "ma2", "adx", "ci", "rsi", "mfi", "cci", "stoch", "cmo", "obv", "macd", "vol", "atr", "st", "bb", "sup", "eb", "sar")]
        check("every indicator except MA Crossover and Support / Resistance has a Timeframe box", sorted(tf_ids) == sorted(want), str(sorted(set(want) ^ set(tf_ids))))
        opts = page.eval_on_selector_all("#hub_rsi_tf option", "os => os.map(o => o.value)")
        check("Timeframe list = Chart + every exchange candle size", [o for o in opts if o] == TFS, str(opts))
        check("an older plan (no timeframe saved) shows Chart", page.input_value("#hub_rsi_tf") == "Chart" and page.input_value("#hub_ma1_tf") == "Chart")
        # MFI
        page.check("#hub_mfi_entry"); wait(200)
        check("MFI ticked: its card shows with the 4 conditions", page.is_visible("#hubcard_mfi") and
              page.eval_on_selector_all("#hub_mfi_modes input[type=checkbox]", "xs => xs.map(x => x.value)")
              == ["Above / below level", "Overbought / oversold", "Divergence", "MFI crosses its own MA"])
        check("MFI: Above / below level is ticked at first and shows Length and Level", vis("mfi_length") and vis("mfi_level") and not vis("mfi_oversold") and not vis("mfi_pivot"))
        only(page, "mfi_modes", "Overbought / oversold")
        check("Overbought / oversold: shows Oversold, Overbought, N", vis("mfi_oversold") and vis("mfi_overbought") and vis("mfi_n") and not vis("mfi_level"))
        only(page, "mfi_modes", "Above / below level")
        page.fill("#hub_mfi_length", "14"); page.fill("#hub_mfi_level", "50")
        page.select_option("#hub_mfi_tf", "4h"); wait(100)
        save()
        m = blk("mfi")
        check("saved: MFI on 4h, length 14, level 50, only its own numbers",
              (m.get("tf"), m.get("length"), m.get("level"), m.get("modes"), m.get("entry")) == ("4h", 14, 50, ["Above / below level"], True)
              and not any(k in m for k in ("oversold", "overbought", "pivot", "lookback", "ma_type")), str(m) + page.inner_text("#hub-msg")[:200])
        check("the review shows the timeframe", "[on 4h candles]" in seen["review"], seen["review"][:300].replace("\n", " | "))
        # old indicator on another timeframe
        page.select_option("#hub_rsi_tf", "1d"); wait(100)
        page.fill("#hub_rsi_length", "14"); page.fill("#hub_rsi_level", "50")
        save()
        check("saved: RSI on 1d", blk("rsi").get("tf") == "1d", str(blk("rsi")) + page.inner_text("#hub-msg")[:200])
        check("saved: an indicator left on Chart keeps tf Chart", blk("ma1").get("tf") in (None, "Chart"), str(blk("ma1")))
        page.select_option("#hub_rsi_tf", ""); wait(100)
        save()
        check("Timeframe left empty: refused with a clear message or saved as Chart",
              page.inner_text("#hub-msg").startswith("Not saved") or blk("rsi").get("tf") in (None, "Chart"), page.inner_text("#hub-msg")[:200] + str(blk("rsi")))
        page.select_option("#hub_rsi_tf", "1d"); wait(100)
        # OBV
        page.check("#hub_obv_entry"); wait(200)
        check("OBV has 3 conditions and no level / oversold / overbought boxes",
              page.eval_on_selector_all("#hub_obv_modes input[type=checkbox]", "xs => xs.map(x => x.value)")
              == ["OBV above / below its own MA", "OBV rising / falling", "Divergence"] and not vis("obv_level") and not vis("obv_oversold") and not vis("obv_overbought"))
        only(page, "obv_modes", "OBV rising / falling")
        check("OBV rising / falling needs no numbers", not any(vis("obv_" + x) for x in ("ma_type", "ma_length", "pivot", "lookback", "n")))
        save()
        check("saved: OBV rising / falling, tf Chart", (blk("obv").get("modes"), blk("obv").get("tf")) == (["OBV rising / falling"], "Chart") and blk("obv").get("entry") is True, str(blk("obv")) + page.inner_text("#hub-msg")[:200])
        only(page, "obv_modes", "OBV above / below its own MA")
        check("OBV above / below its own MA: average boxes show", vis("obv_ma_type") and vis("obv_ma_length"))
        page.select_option("#hub_obv_ma_type", "EMA"); page.fill("#hub_obv_ma_length", "20")
        save()
        check("saved: OBV with its own EMA 20", (blk("obv").get("ma_type"), blk("obv").get("ma_length")) == ("EMA", 20), str(blk("obv")) + page.inner_text("#hub-msg")[:200])
        # CCI negative levels and a refused band
        page.check("#hub_cci_entry"); wait(200)
        only(page, "cci_modes", "Overbought / oversold")
        page.fill("#hub_cci_length", "20"); page.fill("#hub_cci_oversold", "-100"); page.fill("#hub_cci_overbought", "100"); page.fill("#hub_cci_n", "2")
        save()
        c = blk("cci")
        check("saved: CCI oversold -100 / overbought 100", (c.get("oversold"), c.get("overbought"), c.get("n")) == (-100, 100, 2), str(c) + page.inner_text("#hub-msg")[:200])
        page.fill("#hub_cci_oversold", "100"); page.fill("#hub_cci_overbought", "-100"); save()
        check("CCI oversold above overbought is refused", page.inner_text("#hub-msg").startswith("Not saved"), page.inner_text("#hub-msg")[:200])
        page.fill("#hub_cci_oversold", "-100"); page.fill("#hub_cci_overbought", "100")
        # Stochastic two numbers, CMO
        page.check("#hub_stoch_entry"); page.check("#hub_cmo_entry"); wait(200)
        check("Stochastic shows %K length and smoothing; CMO shows length", vis("stoch_length") and vis("stoch_smooth") and vis("cmo_length") and not vis("cmo_smooth"))
        page.fill("#hub_stoch_length", "14"); page.fill("#hub_stoch_smooth", "3"); page.fill("#hub_stoch_level", "50")
        page.fill("#hub_cmo_length", "9"); page.fill("#hub_cmo_level", "0")
        save()
        check("saved: Stochastic 14 / 3 and CMO 9", (blk("stoch").get("length"), blk("stoch").get("smooth"), blk("cmo").get("length")) == (14, 3, 9), str(blk("stoch")) + str(blk("cmo")) + page.inner_text("#hub-msg")[:200])
        # counter trend list and the saved plan reload
        page.reload(); wait(600)
        open_pair()
        check("after a reload: MFI is ticked and on 4h, RSI on 1d, OBV on its EMA 20",
              page.is_checked("#hub_mfi_entry") and page.input_value("#hub_mfi_tf") == "4h" and page.input_value("#hub_rsi_tf") == "1d"
              and page.input_value("#hub_obv_ma_length") == "20", "")
        check("no JavaScript errors", not errors, str(errors[:3]))
        check("no 'No help text' warning", not warns, str(warns[:3]))
        b.close()
    ok = all(res)
    print(f"\n{sum(res)} passed, {len(res) - sum(res)} failed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
