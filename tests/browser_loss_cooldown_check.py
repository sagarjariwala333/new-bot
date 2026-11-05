"""
browser_loss_cooldown_check.py - OPTIONAL real-browser check of the Cooldown after a loss and Loss limit tabs and the
Loss limits box (account / all accounts) (2026-11-01). Arbitrary test numbers only.
Backend = the REAL plan routes of app/api; no exchange calls.  Run:  python tests/browser_loss_cooldown_check.py
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_loss_cooldown")
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
from app import loss_limits as LL
from tests import _test_values as tv
from tests._hub_plan import make_plan

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"
store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
L.LIBRARY_FILE = TEST_DATA_DIR / "strategy_library.json"
LL.LIMITS_FILE = TEST_DATA_DIR / "loss_limits.json"
NONE4 = {"daily": None, "weekly": None, "daily_pct": None, "weekly_pct": None}
store = store_module.Store(); okx_store = okx_store_module.OKXStore()
api.store = store; okx_api.okx_store = okx_store


class _Mgr:
    def get(self, *a): return None
    async def restart(self, *a): pass


api.manager = _Mgr()
acc = store.create_account("Test", "k", "s", True)
PLAN1 = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": "% of ATR", "value": 200.0, "atr_length": 14},
                  tp={"on": True, "basis": "% of ATR", "atr_length": 14,
                      "levels": [{"on": True, "value": 100.0, "pct": 50.0}, {"on": True, "value": 200.0, "pct": 50.0},
                                 {"on": False, "value": 300.0, "pct": 1.0}]}, name="Loss test")
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
        if path == "/api/loss-limits" and method == "GET": return 200, asyncio.run(api.get_loss_limits())
        if path == "/api/loss-limits" and method == "POST": return 200, asyncio.run(api.save_loss_limits(Body(payload)))
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
    def save():
        dismiss(); page.click("#hub-save-btn"); wait(300)
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
    def picked(fn): return page.get_attribute(f'.hubx-item[data-fn="{fn}"], #hub-menu button[data-fn="{fn}"]', "aria-pressed") == "true"
    def setv(i, v): page.fill("#hub_" + i, str(v))
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(200)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        names = page.eval_on_selector_all("#hub-menu button", "bs => bs.map(b => b.querySelector('.hubx-iname').firstChild.textContent)")
        check("menu: the two functions are still where they were (Exit when comes after them)", names[-4:-1] == ["Cooldown after a loss", "Loss limit", "Exit when"] and len(names) == 18, str(names))
        check("an older plan loads with both NOT picked", not picked("cooldown") and not picked("losslimit"))
        # pick both
        page.click("#hub-edit-btn"); wait(100)
        page.click('#hub-menu button[data-fn="cooldown"]'); wait(60)
        page.click('#hub-menu button[data-fn="losslimit"]'); wait(60)
        check("both can be picked", picked("cooldown") and picked("losslimit"))
        page.click("#hub-confirm-btn"); wait(200)
        page.click('#hub-menu button[data-fn="cooldown"]'); wait(150)
        check("Cooldown tab shows its two boxes", vis("cd_candles") and vis("cd_losses"))
        check("no starting numbers in them", page.input_value("#hub_cd_candles") == "" and page.input_value("#hub_cd_losses") == "")
        setv("cd_candles", 3)
        page.click('#hub-menu button[data-fn="losslimit"]'); wait(150)
        check("Loss limit tab shows daily and weekly (USDT and %)", vis("loss_daily") and vis("loss_weekly") and vis("loss_daily_pct") and vis("loss_weekly_pct"))
        check("no starting numbers there either", page.input_value("#hub_loss_daily") == "" and page.input_value("#hub_loss_weekly") == ""
              and page.input_value("#hub_loss_daily_pct") == "" and page.input_value("#hub_loss_weekly_pct") == "")
        # a Loss limit with no amount is refused by the plan check, nothing is saved
        before = json.dumps(plan_of("AAAUSDT"), sort_keys=True)
        save()
        msg = page.inner_text("#hub-msg")
        check("Loss limit with no amount is refused and the pair's plan is not changed",
              "daily and / or a weekly" in msg and json.dumps(plan_of("AAAUSDT"), sort_keys=True) == before, msg[:300])
        setv("loss_daily", 20)
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review lists the cooldown and the loss limit", "Cooldown after a loss: 3 candle close(s)" in rv and "Loss limit (this pair" in rv and "20 USDT a day" in rv, rv[-700:])
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        pl = plan_of("AAAUSDT") or {}
        check("saved: cooldown 3 candles (no losses-in-a-row key), loss limit daily 20 (no weekly key)",
              pl.get("cooldown") == {"on": True, "candles": 3} and pl.get("loss_limit") == {"on": True, "daily": 20}, str(pl.get("cooldown")) + str(pl.get("loss_limit")))
        check("the rest of the plan is unchanged", pl.get("tp", {}).get("on") is True and pl.get("sl", {}).get("on") is True)
        # reload
        page.click('#hub-menu button[data-fn="pair"]'); wait(100)
        page.select_option("#hub-pair-select", "BBBUSDT"); wait(400)
        page.click('#hub-menu button[data-fn="pair"]'); wait(100)
        page.select_option("#hub-pair-select", "AAAUSDT"); wait(500)
        check("reloaded: both picked", picked("cooldown") and picked("losslimit"))
        page.click('#hub-menu button[data-fn="cooldown"]'); wait(100)
        check("reloaded: cooldown 3, losses empty", page.input_value("#hub_cd_candles") == "3" and page.input_value("#hub_cd_losses") == "")
        setv("cd_losses", 2)
        page.click('#hub-menu button[data-fn="losslimit"]'); wait(100)
        check("reloaded: daily 20, weekly empty", page.input_value("#hub_loss_daily") == "20" and page.input_value("#hub_loss_weekly") == "")
        setv("loss_weekly", 60)
        save()
        pl = plan_of("AAAUSDT") or {}
        check("saved with losses in a row 2 and weekly 60",
              pl.get("cooldown") == {"on": True, "candles": 3, "after_losses": 2} and pl.get("loss_limit") == {"on": True, "daily": 20, "weekly": 60}, str(pl.get("cooldown")) + str(pl.get("loss_limit")))
        # the % boxes (2026-11-02)
        setv("loss_daily_pct", 5); setv("loss_weekly_pct", 10)
        page.click("#hub-save-btn"); wait(300)
        rv = page.inner_text("#hub-review-body") if page.is_visible("#dlg-hub-review") else ""
        check("review names the % limits", "5 % of the balance a day" in rv and "10 % of the balance a week" in rv and "20 USDT a day" in rv, rv[-600:])
        if page.is_visible("#dlg-hub-review"):
            page.click("#hub-review-apply"); wait(800)
        pl = plan_of("AAAUSDT") or {}
        check("saved: USDT and % together", pl.get("loss_limit") == {"on": True, "daily": 20, "weekly": 60, "daily_pct": 5, "weekly_pct": 10}, str(pl.get("loss_limit")))
        setv("loss_daily_pct", 101); save()
        msg = page.inner_text("#hub-msg")
        check("a % over 100 is refused and the saved plan is not changed", msg.startswith("Not saved") and (plan_of("AAAUSDT") or {}).get("loss_limit", {}).get("daily_pct") == 5, msg[:200])
        setv("loss_daily_pct", 0); save()
        msg = page.inner_text("#hub-msg")
        check("0 % is refused", msg.startswith("Not saved"), msg[:200])
        page.fill("#hub_loss_daily_pct", ""); page.fill("#hub_loss_weekly_pct", ""); save()
        pl = plan_of("AAAUSDT") or {}
        check("emptied % boxes leave no % key in the plan", pl.get("loss_limit") == {"on": True, "daily": 20, "weekly": 60}, str(pl.get("loss_limit")) + page.inner_text("#hub-msg")[:200])
        page.fill("#hub_loss_daily", ""); page.fill("#hub_loss_weekly", ""); setv("loss_weekly_pct", 7.5); save()
        pl = plan_of("AAAUSDT") or {}
        check("a % alone is enough", pl.get("loss_limit") == {"on": True, "weekly_pct": 7.5}, str(pl.get("loss_limit")) + page.inner_text("#hub-msg")[:200])
        setv("loss_daily", 20); setv("loss_weekly", 60); page.fill("#hub_loss_weekly_pct", ""); save()
        # info icons
        for key, word, tab in (("fn.cooldown", "skipped", "cooldown"), ("cd_candles", "candle closes", "cooldown"),
                               ("cd_losses", "in a row", "cooldown"), ("fn.losslimit", "never touches", "losslimit"),
                               ("loss_daily", "00:00", "losslimit"), ("loss_weekly", "Monday", "losslimit"),
                               ("loss_daily_pct", "balance at the start", "losslimit"), ("loss_weekly_pct", "Monday", "losslimit"),
                               ("lossbox.open", "Base V3", "losslimit")):
            page.click(f'#hub-menu button[data-fn="{tab}"]'); wait(120)
            loc = page.locator(f'.info-i[data-help="{key}"]:visible')
            ok = loc.count() > 0
            txt = ""
            if ok:
                loc.first.click(); wait(150); ok = page.is_visible("#dlg-help"); txt = page.inner_text("#dlg-help") if ok else ""
                if ok: page.click("#dlg-help button.ghost"); wait(100)
            check(f"info icon {key} opens a text with '{word}'", ok and word in txt, txt[:160])
        # switching both off saves them as off
        page.click("#hub-edit-btn"); wait(100)
        page.click('#hub-menu button[data-fn="cooldown"]'); wait(60)
        page.click('#hub-menu button[data-fn="losslimit"]'); wait(60)
        page.click("#hub-confirm-btn"); wait(200)
        save()
        pl = plan_of("AAAUSDT") or {}
        check("both off: saved as off, nothing else needed", pl.get("cooldown") == {"on": False} and pl.get("loss_limit") == {"on": False}, str(pl.get("cooldown")) + str(pl.get("loss_limit")) + page.inner_text("#hub-msg")[:200])

        # ---------------- the Loss limits box (account / all accounts)
        check("button next to Presets", page.is_visible("#hub-lossbox-open-btn"))
        page.click("#hub-lossbox-open-btn"); wait(500)
        check("box opens", page.is_visible("#dlg-lossbox"))
        body = page.inner_text("#loss-body")
        check("it lists all accounts together and this account", "All accounts together" in body and "Account: Test" in body, body[:300])
        check("both are empty at first", page.input_value("#ll-entire-daily") == "" and page.input_value(f"#ll-acc-{acc.id}-weekly") == ""
              and page.input_value("#ll-entire-daily_pct") == "" and page.input_value(f"#ll-acc-{acc.id}-weekly_pct") == "")
        check("each limit has a USDT box and a % box", all(page.is_visible(x) for x in (
            "#ll-entire-daily", "#ll-entire-daily_pct", "#ll-entire-weekly", "#ll-entire-weekly_pct",
            f"#ll-acc-{acc.id}-daily", f"#ll-acc-{acc.id}-daily_pct", f"#ll-acc-{acc.id}-weekly", f"#ll-acc-{acc.id}-weekly_pct")))
        check("it shows the result so far", "Closed result so far today: 0.00 USDT" in body)
        page.fill("#ll-entire-daily", "100"); page.fill(f"#ll-acc-{acc.id}-daily", "30"); page.fill(f"#ll-acc-{acc.id}-weekly", "80")
        page.click("#loss-save-btn"); wait(600)
        d = LL.load()
        check("saved on the server: entire daily 100; account daily 30 weekly 80",
              d["entire"] == dict(NONE4, daily=100.0) and d["accounts"].get(acc.id) == dict(NONE4, daily=30.0, weekly=80.0), str(d))
        check("it says saved and shows the numbers again", "Saved" in page.inner_text("#loss-msg") and page.input_value(f"#ll-acc-{acc.id}-daily") == "30")
        page.fill("#ll-entire-daily", "0"); page.click("#loss-save-btn"); wait(500)
        check("0 is refused with a message and nothing changes", "Not saved" in page.inner_text("#loss-msg") and LL.load()["entire"]["daily"] == 100.0, page.inner_text("#loss-msg")[:200])
        page.fill("#ll-entire-daily", ""); page.fill(f"#ll-acc-{acc.id}-daily", ""); page.fill(f"#ll-acc-{acc.id}-weekly", "")
        page.click("#loss-save-btn"); wait(500)
        d = LL.load()
        check("emptied boxes clear the limits", d["entire"] == NONE4 and acc.id not in d["accounts"], str(d))
        # the % boxes of the box
        page.fill("#ll-entire-daily_pct", "3"); page.fill(f"#ll-acc-{acc.id}-weekly_pct", "12.5")
        page.click("#loss-save-btn"); wait(600)
        d = LL.load()
        check("saved on the server: entire daily 3 %; account weekly 12.5 %",
              d["entire"] == dict(NONE4, daily_pct=3.0) and d["accounts"].get(acc.id) == dict(NONE4, weekly_pct=12.5), str(d))
        check("the % boxes show the numbers again and say the balance is saved at the first check",
              page.input_value("#ll-entire-daily_pct") == "3" and page.input_value(f"#ll-acc-{acc.id}-weekly_pct") == "12.5"
              and "saved at the first check" in page.inner_text("#loss-body"), page.inner_text("#loss-body")[:400])
        LL.record_balance(acc.id, 2000.0)
        page.click("#dlg-lossbox button.ghost"); wait(100)
        page.click("#hub-lossbox-open-btn"); wait(500)
        body = page.inner_text("#loss-body")
        check("once the start balance is saved the box shows the % as USDT", "= 60.00 USDT (of 2000.00 USDT at the start of today)" in body and "= 250.00 USDT (of 2000.00 USDT at the start of the week)" in body, body[:700])
        page.fill("#ll-entire-daily_pct", "101"); page.click("#loss-save-btn"); wait(500)
        check("a % over 100 is refused with a message and nothing changes", "Not saved" in page.inner_text("#loss-msg") and LL.load()["entire"]["daily_pct"] == 3.0, page.inner_text("#loss-msg")[:200])
        page.fill("#ll-entire-daily_pct", ""); page.fill(f"#ll-acc-{acc.id}-weekly_pct", "")
        page.click("#loss-save-btn"); wait(500)
        d = LL.load()
        check("emptied % boxes clear the limits", d["entire"] == NONE4 and acc.id not in d["accounts"], str(d))
        page.click("#dlg-lossbox button.ghost"); wait(100)
        check("box closes", not page.is_visible("#dlg-lossbox"))
        page.set_viewport_size({"width": 390, "height": 800}); wait(200)
        page.click("#hub-lossbox-open-btn"); wait(500)
        check("phone: the box fits (no sideways scroll)", page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
