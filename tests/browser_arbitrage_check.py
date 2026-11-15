"""
browser_arbitrage_check.py - OPTIONAL real-browser check of the Arbitrage tab (Step 1: settings + read-only board).
Needs Playwright + Chromium. The routes are the REAL arb service functions; the board is filled by hand (no exchange calls).
Run:  python tests/browser_arbitrage_check.py
"""
import asyncio, json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_arb")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

import tests._aiohttp_stub  # noqa
import tests._pydantic_stub  # noqa
import tests._fastapi_stub  # noqa
from app.arb import service
from app.arb.settings import ArbSettingsStore

service.arb_settings.path = TEST_DATA_DIR / "arb" / "settings.json"
# seed the mock trader with one closed trade and one open trade (fake prices, no exchange)
import copy
from app.arb import settings as _st
from app.arb.instruments import Instr
service.trader.set_folder(TEST_DATA_DIR / "arb")


class _Scn:
    rules = {("binance", "futures"): {"BTCUSDT": Instr("binance", "futures", "BTCUSDT", "BTC", "USDT", "0.001", "0.001", "0.1", 5, 1)},
             ("okx", "futures"): {"BTC-USDT-SWAP": Instr("okx", "futures", "BTC-USDT-SWAP", "BTC", "USDT", "0.01", "0.01", "0.1", 0, "0.01")}}
    books = {("binance", "futures"): {"BTCUSDT": {"bid": 99990.0, "ask": 100000.0, "bq": 10, "aq": 10}},
             ("okx", "futures"): {"BTC-USDT-SWAP": {"bid": 100400.0, "ask": 100410.0, "bq": 10, "aq": 10}}}
    funding, volumes, stamp = {}, {}, 1

    @staticmethod
    def book_age(key): return 0.0

    universe = {"spatial": {"futures": ["BTC"]}, "funding": {}, "triangular": {}}


_s = copy.deepcopy(_st.DEFAULTS)
_s["modules"]["spatial"].update(markets=["futures"], trade_size_usdt=1000, slippage_pct=0)
_s["modules"]["funding"]["enabled"] = False
service.trader.step(_s, _Scn)                                   # opens a mock trade
_Scn.books[("binance", "futures")]["BTCUSDT"].update(bid=100200.0, ask=100210.0)
_Scn.books[("okx", "futures")]["BTC-USDT-SWAP"].update(bid=100190.0, ask=100200.0)
service.trader.step(_s, _Scn)                                   # closes it with a profit
_Scn.books[("binance", "futures")]["BTCUSDT"].update(bid=99990.0, ask=100000.0)
_Scn.books[("okx", "futures")]["BTC-USDT-SWAP"].update(bid=100400.0, ask=100410.0)
service.trader.step(_s, _Scn)                                   # opens a second one, left open

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
POSTS = []
service.scanner.board = {
    "spatial": [{"base": "BTC", "market": "futures", "buy": "binance", "sell": "okx", "ask": 100.0, "bid": 100.6,
                 "aq": 5, "bq": 4, "gap": 0.6, "net": 0.36}],
    "funding": [{"base": "ETH", "ex": "okx", "rate": 0.05, "basis": 0.1, "fees": 0.3, "periods": 6.0, "net": -0.15}],
    "triangular": []}
service.scanner.status.update({"last_scan_utc": "2026-11-15 10:00:00", "scan_ms": 420, "notes": ["Triangular is LOCKED: tick 'confirm_enable' to see its board."]})


def route(method, path, payload):
    try:
        if path == "/api/auth/status": return 200, {"ok": True}
        if path in ("/api/accounts", "/api/okx/accounts"): return 200, []
        if path in ("/api/status", "/api/okx/status"): return 200, {"instances": [], "startup_failures": {}}
        if path == "/healthz": return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/arb" and method == "GET": return 200, service.get_state()
        if path == "/api/arb/mock/reset" and method == "POST":
            return 200, asyncio.run(service.reset_mock()) if payload.get("confirm") is True else (400, {})
        try:
            if path == "/api/arb/keys" and method == "POST":
                slot = payload.pop("slot", "")
                return 200, service.save_key(slot, payload)
            if path == "/api/arb/stop" and method == "POST":
                return 200, service.set_stop(payload.get("on") is True)
            if path == "/api/arb/triangular" and method == "POST":
                return 200, service.triangular_lock(None if payload.get("lock") is True else payload.get("typed"))
            if path == "/api/arb/keys/check" and method == "POST":
                return 200, asyncio.run(service.check_keys())
        except service.ArbSaveError as e:
            return 400, {"detail": {"errors": e.errors}}
        if path == "/api/arb/settings" and method == "POST":
            POSTS.append(payload)
            try:
                confirm = payload.pop("confirm_live", None)
                return 200, asyncio.run(service.save_settings(payload, confirm))
            except service.ArbSaveError as e:
                return 400, {"detail": {"errors": e.errors}}
        return 404, {"detail": f"unhandled {method} {path}"}
    except Exception as e:
        return 500, {"detail": str(e)}


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
    def do_POST(self): self._go("POST")


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1400, "height": 1000})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); page.wait_for_timeout(600)
        check("Arbitrage tab is in the menu", page.is_visible("#maintab-arb"))
        page.click("#maintab-arb"); page.wait_for_timeout(600)
        check("tab page opens", page.is_visible("#tabpage-arb") and not page.is_visible("#tabpage-chart"))
        check("status says scanner OFF", "Scanner: OFF" in page.inner_text("#arb-status"), page.inner_text("#arb-status"))
        check("lock note is shown", "LOCKED" in page.inner_text("#arb-notes"))
        check("intro says the scanner only reads and live needs the panel", "READS public prices" in page.inner_text("#tabpage-arb") and "Live trading" in page.inner_text("#tabpage-arb"))
        OPEN = "document.querySelectorAll('#arb-form details').forEach(d => d.open = true)"
        page.evaluate(OPEN)
        board = page.inner_text("#arb-board")
        check("spatial row is shown with gap and net", "BTC" in board and "+0.600%" in board and "+0.360%" in board, board)
        check("funding row is shown", "ETH" in board and "+0.0500%" in board and "6.0" in board, board)
        check("empty triangular says nothing to show", "Nothing to show" in board)
        check("live is selectable", page.evaluate("[...document.querySelectorAll('#arb-form select[id$=\"__mode\"] option[value=live]')].every(o=>!o.disabled)") and
              page.evaluate("document.querySelectorAll('#arb-form select[id$=\"__mode\"]').length") == 3)
        check("Top N shows the default 30", page.input_value("#arbf_pairs__top_n") == "30")
        check("risk limits are blank (must be filled before live)", page.input_value("#arbf_risk__max_size_per_trade_usdt") == "" and page.input_value("#arbf_risk__daily_loss_limit_usdt") == "")
        check("triangular is off and unconfirmed", not page.is_checked("#arbf_modules__triangular__enabled") and not page.is_checked("#arbf_modules__triangular__confirm_enable"))
        check("scanner switch is off", not page.is_checked("#arbf_scanner_on"))
        check("all 9 groups are present", page.locator("#arb-form details").count() == 9, str(page.locator("#arb-form details").count()))
        check("every field has an info icon", page.locator("#arb-form label .info-i").count() >= 50, str(page.locator("#arb-form label .info-i").count()))
        page.click("#tabpage-arb h2 .info-i"); page.wait_for_timeout(200)
        check("info icon opens the explanation", page.is_visible("#dlg-help") and "Arbitrage tab" in page.inner_text("#help-body"))
        page.evaluate("document.getElementById('dlg-help').close()")
        # ---- mock trading panel
        mock = page.inner_text("#arb-mock")
        check("mock wallets table names the engine and shows running", "spatial/futures/mock" in mock and "running" in mock, mock[:300])
        check("mock: 1 closed trade, 1 win, positive net", "1 (1 wins)" in mock, mock[:400])
        check("open mock trade is listed with its legs", "open" in mock and "buy binance" in mock and "sell okx" in mock, mock)
        check("daily result row for today", "Daily result" in mock and "spatial" in mock)
        check("last closed trades shows the reason", "take_profit" in mock, mock)
        check("mock section says pretend money", "PRETEND" in page.inner_text("#tabpage-arb"))
        check("no mock problems box", not page.is_visible("#arb-mock-problems"))
        page.click("#arb-reset-btn"); page.wait_for_timeout(100)
        check("reset asks first", page.is_visible("#arb-reset-ask") and not page.is_visible("#arb-reset-btn"))
        page.click("#arb-reset-no"); page.wait_for_timeout(100)
        check("cancel keeps everything", page.is_visible("#arb-reset-btn") and "1 (1 wins)" in page.inner_text("#arb-mock"))
        # typing is not overwritten by the 3 second refresh
        page.fill("#arbf_pairs__top_n", "17"); page.wait_for_timeout(3500)
        check("typing is kept while the board refreshes", page.input_value("#arbf_pairs__top_n") == "17")
        # bad save -> every problem listed, nothing saved
        page.fill("#arbf_loop_seconds", "abc"); page.fill("#arbf_pairs__top_n", "0")
        page.click("#arb-save"); page.wait_for_timeout(500)
        errs = page.inner_text("#arb-errors")
        check("bad values: all problems listed in plain words", page.is_visible("#arb-errors") and "loop_seconds" in errs and "pairs.top_n" in errs and "Not saved" in errs, errs)
        check("bad values: nothing written", not service.arb_settings.path.exists())
        # undo
        page.click("text=Undo my edits"); page.wait_for_timeout(300); page.evaluate(OPEN)
        check("Undo restores the saved values and hides errors", page.input_value("#arbf_pairs__top_n") == "30" and not page.is_visible("#arb-errors"))
        # good save
        page.evaluate(OPEN)
        page.fill("#arbf_pairs__top_n", "12"); page.fill("#arbf_pairs__include", "btc, eth, sol")
        page.evaluate("document.querySelector('input.arbf_modules__spatial__markets[value=spot]').checked = false")
        page.fill("#arbf_risk__max_size_per_trade_usdt", "25")
        page.select_option("#arbf_orders__type", "market")
        page.click("#arb-save"); page.wait_for_timeout(600)
        check("good save: Saved message", "Saved" in page.inner_text("#arb-saved"), page.inner_text("#arb-saved"))
        sent = POSTS[-1]
        check("sent numbers are numbers", sent["pairs"]["top_n"] == 12 and sent["risk"]["max_size_per_trade_usdt"] == 25 and sent["risk"]["daily_loss_limit_usdt"] is None, str(sent["risk"]))
        check("coin list is upper-case and split", sent["pairs"]["include"] == ["BTC", "ETH", "SOL"], str(sent["pairs"]["include"]))
        check("markets list follows the ticks", sent["modules"]["spatial"]["markets"] == ["futures"])
        check("order type saved", sent["orders"]["type"] == "market")
        saved = json.loads(service.arb_settings.path.read_text())
        check("file written on the server", saved["pairs"]["top_n"] == 12 and saved["orders"]["type"] == "market")
        page.reload(); page.wait_for_timeout(500); page.click("#maintab-arb"); page.wait_for_timeout(500); page.evaluate(OPEN)
        check("values survive a page reload", page.input_value("#arbf_pairs__top_n") == "12" and page.input_value("#arbf_risk__max_size_per_trade_usdt") == "25")
        # ---- live gates, in the order the owner meets them ----
        page.evaluate("(() => { const s = document.querySelector('#arbf_modules__spatial__mode'); s.value = 'live'; })()")
        page.click("#arb-save"); page.wait_for_timeout(500)
        errs = page.inner_text("#arb-errors")
        check("live is refused until the risk boxes are filled", "must be filled in" in errs, errs)
        check("margin cap box exists and is blank", page.input_value("#arbf_risk__margin_cap_usdt") == "")
        page.fill("#arbf_risk__max_size_per_trade_usdt", "50"); page.fill("#arbf_risk__daily_loss_limit_usdt", "20"); page.fill("#arbf_risk__margin_cap_usdt", "100")
        page.click("#arb-save"); page.wait_for_timeout(500)
        errs = page.inner_text("#arb-errors")
        check("live is refused without typing LIVE", "Type LIVE" in errs, errs)
        check("live is refused without keys", "API key" in errs, errs)
        # keys
        page.fill("#arbk_binance_futures_key", "BINKEY123456"); page.fill("#arbk_binance_futures_secret", "BINSECRET")
        page.click("#arbk_binance_futures_key ~ button"); page.wait_for_timeout(500)
        keys_txt = page.inner_text("#arb-keys")
        check("key saved: only last 4 shown, secret never shown", "3456" in keys_txt and "BINSECRET" not in page.content() and "BINKEY123456" not in page.content(), keys_txt)
        check("typed key fields are emptied after saving", page.input_value("#arbk_binance_futures_key") == "")
        page.fill("#arbk_okx_key", "OKXKEY9999"); page.fill("#arbk_okx_secret", "OKXSEC"); page.fill("#arbk_okx_passphrase", "OKXPASS")
        page.click("#arbk_okx_passphrase ~ button"); page.wait_for_timeout(500)
        check("OKX key saved", "9999" in page.inner_text("#arb-keys"))
        page.fill("#arbk_okx_key", "x y"); page.fill("#arbk_okx_secret", "a"); page.fill("#arbk_okx_passphrase", "b")
        page.click("#arbk_okx_passphrase ~ button"); page.wait_for_timeout(400)
        check("bad key (spaces) is refused in plain words", "spaces" in page.inner_text("#arbk_okx_msg"), page.inner_text("#arbk_okx_msg"))
        page.fill("#arb-confirm-live", "live"); page.click("#arb-save"); page.wait_for_timeout(500)
        check("lowercase live is not enough", "Type LIVE" in page.inner_text("#arb-errors"))
        page.fill("#arb-confirm-live", "LIVE"); page.click("#arb-save"); page.wait_for_timeout(600)
        check("saved as live after LIVE + keys + limits", page.evaluate("ARB.state.settings.modules.spatial.mode") == "live" and page.inner_text("#arb-errors").strip() == "", page.inner_text("#arb-errors"))
        check("LIVE box is emptied after saving", page.input_value("#arb-confirm-live") == "")
        check("checklist says all present", "all settings and keys present" in page.inner_text("#arb-checklist"), page.inner_text("#arb-checklist"))
        # stop button
        page.click("#arb-stop-on"); page.wait_for_timeout(500)
        check("Stop button turns the stop ON", "Stop is ON" in page.inner_text("#arb-stop") and os.path.exists(TEST_DATA_DIR / "arb" / "STOP.txt"))
        check("banner shows the stop", "Stop is ON" in page.inner_text("#arb-live-banner"))
        page.click("#arb-stop-off"); page.wait_for_timeout(500)
        check("Stop button turns it off again", "New trades allowed" in page.inner_text("#arb-stop") and not os.path.exists(TEST_DATA_DIR / "arb" / "STOP.txt"))
        # triangular second lock
        page.fill("#arb-tri-yes", "yes"); page.click("#arb-tri-unlock"); page.wait_for_timeout(400)
        check("triangular: lowercase yes is refused", "YES" in page.inner_text("#arb-tri-msg"), page.inner_text("#arb-tri-msg"))
        page.fill("#arb-tri-yes", "YES"); page.click("#arb-tri-unlock"); page.wait_for_timeout(400)
        check("triangular: YES without the first lock is refused", "First switch triangular on" in page.inner_text("#arb-tri-msg"), page.inner_text("#arb-tri-msg"))
        # clear a key
        page.click("#arbk_okx_passphrase ~ button:nth-of-type(2)"); page.wait_for_timeout(400)
        check("key can be cleared", "not entered" in page.inner_text("#arb-keys"), page.inner_text("#arb-keys"))
        # key check with nothing usable is read-only and says so
        page.click("#arbk_binance_futures_key ~ button:nth-of-type(2)"); page.wait_for_timeout(400)
        page.click("#arb-keys-check"); page.wait_for_timeout(700)
        check("check keys with no key says not entered", "not entered" in page.inner_text("#arb-keys-result"), page.inner_text("#arb-keys-result"))
        # reset (last, it deletes the seeded mock results)
        page.click("#arb-reset-btn"); page.click("#arb-reset-yes"); page.wait_for_timeout(600)
        mock2 = page.inner_text("#arb-mock")
        check("reset: results gone", "1 (1 wins)" not in mock2 and "Deleted" not in mock2 and "deleted" in page.inner_text("#arb-reset-msg"), page.inner_text("#arb-reset-msg"))
        check("reset: files removed on the server", not [f for f in os.listdir(TEST_DATA_DIR / "arb") if f.startswith("state_") or f == "trades.csv"])
        # timer stops on leaving the tab
        check("board refresh is running on the tab", page.evaluate("ARB.timer !== null"))
        page.click("#maintab-trading"); page.wait_for_timeout(200)
        check("board refresh stops when the tab is left", page.evaluate("ARB.timer === null"))
        check("other tabs still switch", page.is_visible("#tabpage-trading") and not page.is_visible("#tabpage-arb"))
        check("no page errors", not errors, str(errors))
        b.close()
    server.shutdown()
    print(f"\n{sum(res)}/{len(res)} passed")
    sys.exit(0 if all(res) else 1)


if __name__ == "__main__":
    main()
