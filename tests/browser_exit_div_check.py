"""
browser_exit_div_check.py - OPTIONAL real-browser check of Exit when -> Divergence (RSI / MFI / CCI) (2026-11-17).
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_exit_div_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_exit_div")
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
PLAN1 = make_plan(ind_ma1={"entry": True}, ind_bb={"entry": True, "length": 20, "mult": 2.0, "n": 2},
                  ind_sup={"flip": True, "modes": ["Trend direction", "Fresh flip within N candles"], "atr_length": 10, "factor": 3.0, "n": 4},
                  exit_when={"on": True, "signal": "MA 2 only (the slower one) curves against the trade", "after": "Close and flip to the other side"},
                  trail={"on": True, "type": "Lowest low / highest high", "length": 10},
                  name="Exit bb sup")
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
    """Confirm needs a Start-as choice when that box is shown (2026-11-09): pick Follow, then confirm."""
    page.wait_for_timeout(150)
    if page.is_visible("#hub-startas-box") and not page.is_checked("#startas-follow") and not page.is_checked("#startas-new"):
        page.click("#startas-follow"); page.wait_for_timeout(120)
    page.click("#hub-review-apply")


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
            apply_review(page); wait(800)
    def fn(i): page.click(f'.hubx-item[data-fn="{i}"]'); wait(150)
    DIV = "Divergence (RSI / MFI / CCI / MACD / Stochastic / OBV / CMO, set below)"
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        fn("exitwhen")
        sig = page.eval_on_selector_all("#hub_exit_signal option", "o => o.map(x => x.value)")
        check("Signal has the 3 choices, Divergence last", [s for s in sig if s][-1:] == [DIV] and len([s for s in sig if s]) == 3, str(sig))
        check("no divergence boxes while the signal is an MA one", not vis("exdiv_rsi_on") and not vis("exdiv_combine"))
        page.select_option("#hub_exit_signal", DIV); wait(200); dismiss()
        check("combine / keep / turn / price show", all(vis(x) for x in ("exdiv_combine", "exdiv_keep", "exdiv_turn", "exdiv_price")))
        check("all 7 on/off ticks show, all OFF", all(vis(f"exdiv_{k}_on") and not page.is_checked(f"#hub_exdiv_{k}_on") for k in ("rsi", "mfi", "cci", "macd", "stoch", "obv", "cmo")))
        check("numbers are hidden while an oscillator is off", not any(vis(f"exdiv_{k}_{f}") for k in ("rsi", "mfi", "cci", "macd", "stoch", "obv", "cmo") for f in ("tf", "length", "fast", "pivot", "lookback", "zone", "ob", "os")))
        page.check("#hub_exdiv_rsi_on"); wait(150); dismiss()
        check("RSI ticked: its timeframe / length / pivot / look-back / stretched tick show", all(vis(f"exdiv_rsi_{f}") for f in ("tf", "length", "pivot", "lookback", "zone")))
        check("RSI levels hidden until a tick needs them", not vis("exdiv_rsi_ob") and not vis("exdiv_rsi_os"))
        check("MFI numbers still hidden", not vis("exdiv_mfi_length"))
        page.check("#hub_exdiv_rsi_zone"); wait(150); dismiss()
        check("stretched tick shows the two RSI levels", vis("exdiv_rsi_ob") and vis("exdiv_rsi_os"))
        page.fill("#hub_exdiv_rsi_length", "14"); page.fill("#hub_exdiv_rsi_pivot", "5"); page.fill("#hub_exdiv_rsi_lookback", "60")
        page.fill("#hub_exdiv_rsi_ob", "70"); page.fill("#hub_exdiv_rsi_os", "30")
        page.select_option("#hub_exdiv_combine", "ANY"); page.fill("#hub_exdiv_keep", "3")
        page.select_option("#hub_exit_after", "Close and wait for a fresh signal")
        # CCI too
        page.check("#hub_exdiv_cci_on"); wait(150); dismiss()
        page.fill("#hub_exdiv_cci_length", "20"); page.fill("#hub_exdiv_cci_pivot", "5"); page.fill("#hub_exdiv_cci_lookback", "60")
        page.check("#hub_exdiv_turn"); wait(150); dismiss()
        check("turn-back tick shows the CCI levels too", vis("exdiv_cci_ob") and vis("exdiv_cci_os"))
        page.fill("#hub_exdiv_cci_ob", "100"); page.fill("#hub_exdiv_cci_os", "-100")
        save()
        ew = (plan_of("AAAUSDT") or {}).get("exit_when") or {}
        dv = ew.get("div") or {}
        check("saved: signal is Divergence", ew.get("on") is True and ew.get("signal") == DIV, str(ew) + page.inner_text("#hub-msg")[:300])
        check("saved: RSI on with its numbers", (dv.get("rsi") or {}).get("on") is True and dv["rsi"].get("length") == 14 and dv["rsi"].get("pivot") == 5 and dv["rsi"].get("lookback") == 60 and dv["rsi"].get("zone") is True and dv["rsi"].get("ob") == 70 and dv["rsi"].get("os") == 30, str(dv))
        check("saved: CCI on with negative lower level", (dv.get("cci") or {}).get("on") is True and dv["cci"].get("os") == -100 and dv["cci"].get("ob") == 100, str(dv))
        check("saved: MFI off", (dv.get("mfi") or {}).get("on") is False, str(dv))
        check("saved: combine ANY, keep 3, turn on, price off", dv.get("combine") == "ANY" and dv.get("keep") == 3 and dv.get("turn") is True and dv.get("price") is False, str(dv))
        # ---- the new oscillators and the timeframe boxes (2026-11-17)
        tfs = page.eval_on_selector_all("#hub_exdiv_rsi_tf option", "o => o.map(x => x.value)")
        check("timeframe box: Chart + every candle size", [t for t in tfs if t] == ["Chart", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "2d", "3d", "5d", "1w", "1M"], str(tfs))
        page.check("#hub_exdiv_macd_on"); wait(150); dismiss()
        check("MACD ticked: timeframe, fast, slow, signal, pivot, look-back show", all(vis(f"exdiv_macd_{f}") for f in ("tf", "fast", "slow", "signal", "pivot", "lookback")))
        check("MACD has no stretched tick and no levels", not any(vis(f"exdiv_macd_{f}") for f in ("zone", "ob", "os")))
        page.check("#hub_exdiv_stoch_on"); wait(150); dismiss()
        check("Stochastic ticked: %K length + smoothing + stretched tick show", all(vis(f"exdiv_stoch_{f}") for f in ("tf", "length", "smooth", "zone")))
        page.check("#hub_exdiv_obv_on"); wait(150); dismiss()
        check("OBV ticked: only timeframe, pivot, look-back", all(vis(f"exdiv_obv_{f}") for f in ("tf", "pivot", "lookback")) and not any(vis(f"exdiv_obv_{f}") for f in ("length", "zone", "ob", "os")))
        page.check("#hub_exdiv_cmo_on"); wait(150); dismiss()
        check("CMO ticked: length + stretched tick show", vis("exdiv_cmo_length") and vis("exdiv_cmo_zone"))
        page.select_option("#hub_exdiv_macd_tf", "3d"); page.fill("#hub_exdiv_macd_fast", "12"); page.fill("#hub_exdiv_macd_slow", "26"); page.fill("#hub_exdiv_macd_signal", "9")
        page.fill("#hub_exdiv_macd_pivot", "3"); page.fill("#hub_exdiv_macd_lookback", "40")
        page.select_option("#hub_exdiv_stoch_tf", "1w"); page.fill("#hub_exdiv_stoch_length", "14"); page.fill("#hub_exdiv_stoch_smooth", "3")
        page.fill("#hub_exdiv_stoch_pivot", "3"); page.fill("#hub_exdiv_stoch_lookback", "40"); page.fill("#hub_exdiv_stoch_ob", "80"); page.fill("#hub_exdiv_stoch_os", "20")
        page.select_option("#hub_exdiv_obv_tf", "1h"); page.fill("#hub_exdiv_obv_pivot", "4"); page.fill("#hub_exdiv_obv_lookback", "50")
        page.select_option("#hub_exdiv_cmo_tf", "15m"); page.fill("#hub_exdiv_cmo_length", "9"); page.fill("#hub_exdiv_cmo_pivot", "4"); page.fill("#hub_exdiv_cmo_lookback", "50")
        page.fill("#hub_exdiv_cmo_ob", "50"); page.fill("#hub_exdiv_cmo_os", "-50")
        page.select_option("#hub_exdiv_rsi_tf", "3d"); page.select_option("#hub_exdiv_cci_tf", "1d")
        save()
        dv = ((plan_of("AAAUSDT") or {}).get("exit_when") or {}).get("div") or {}
        check("saved: MACD 3d with its three lengths", (dv.get("macd") or {}).get("on") is True and dv["macd"].get("tf") == "3d" and (dv["macd"].get("fast"), dv["macd"].get("slow"), dv["macd"].get("signal")) == (12, 26, 9) and "zone" not in dv["macd"] and "ob" not in dv["macd"], str(dv) + page.inner_text("#hub-msg")[:300])
        check("saved: Stochastic 1w, OBV 1h, CMO 15m", dv.get("stoch", {}).get("tf") == "1w" and dv["stoch"].get("smooth") == 3 and dv.get("obv", {}).get("tf") == "1h" and "length" not in dv["obv"] and dv.get("cmo", {}).get("tf") == "15m" and dv["cmo"].get("os") == -50, str(dv))
        check("saved: RSI 3d, CCI 1d", dv.get("rsi", {}).get("tf") == "3d" and dv.get("cci", {}).get("tf") == "1d", str(dv))
        # the MA numbers are not forced for the divergence signal
        fn("ind")
        # reload from the server and see the same boxes
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        fn("exitwhen")
        check("reload: signal Divergence, RSI + CCI ticked, MFI not", page.input_value("#hub_exit_signal") == DIV and page.is_checked("#hub_exdiv_rsi_on") and page.is_checked("#hub_exdiv_cci_on") and not page.is_checked("#hub_exdiv_mfi_on"))
        check("reload: numbers load", page.input_value("#hub_exdiv_rsi_pivot") == "5" and page.input_value("#hub_exdiv_cci_os") == "-100" and page.input_value("#hub_exdiv_keep") == "3")
        # review text
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review shows the divergence line", "Exit when: Divergence against the trade" in rv and "RSI on 3d candles (length 14" in rv and "CCI on 1d candles (length 20" in rv and "MACD histogram on 3d candles (fast 12, slow 26, signal 9" in rv and "OBV on 1h candles (pivot 4" in rv and "turn back" in rv, rv[:900])
        if page.is_visible("#dlg-hub-review"): page.click("#dlg-hub-review button.ghost"); wait(100)
        # saving with nothing ticked is refused with a clear message
        for k_ in ("rsi", "cci", "macd", "stoch", "obv", "cmo"):
            page.uncheck(f"#hub_exdiv_{k_}_on"); wait(100); dismiss()
        page.click("#hub-save-btn"); wait(500)
        if page.is_visible("#dlg-hub-review"):
            apply_review(page); wait(800)
        msg = page.inner_text("#hub-msg")
        ew2 = (plan_of("AAAUSDT") or {}).get("exit_when") or {}
        print("MSG nothing ticked:", msg[:250]); print("PLAN exit_when:", ew2)
        check("nothing ticked: not saved as an empty divergence", not ((ew2.get("div") or {}).get("rsi", {}).get("on") is False and (ew2.get("div") or {}).get("cci", {}).get("on") is False and ew2.get("signal") == DIV and ew2 is not ew), msg[:200])
        for key in ["exdiv_combine", "exdiv_keep", "exdiv_turn", "exdiv_price"] + [f"exdiv_{k}_{f}" for k in ("rsi", "mfi", "cci") for f in ("on", "tf", "length", "pivot", "lookback", "zone", "ob", "os")] + [f"exdiv_macd_{f}" for f in ("on", "tf", "fast", "slow", "signal", "pivot", "lookback")] + [f"exdiv_stoch_{f}" for f in ("on", "tf", "length", "smooth", "zone", "ob", "os")] + ["exdiv_obv_on", "exdiv_obv_tf", "exdiv_obv_pivot", "exdiv_obv_lookback", "exdiv_cmo_on", "exdiv_cmo_tf", "exdiv_cmo_length", "exdiv_cmo_zone", "exdiv_cmo_ob", "exdiv_cmo_os"]:
            ok = bool(page.evaluate("k => (typeof HELP !== 'undefined') && !!HELP[k] && HELP[k].length >= 6", key))
            check(f"help text exists (6 lines): {key}", ok)
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        check("phone: no sideways scroll", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
