"""
browser_info_icons_check.py - OPTIONAL real-browser check of the Strategy Hub tab (needs Playwright + Chromium).
Backend = the REAL plan routes of app/api; no exchange calls. Run:  python tests/browser_info_icons_check.py
"""
import asyncio, json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_icons")
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


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    # Every input / select / checkbox must have an "i" right beside its label. Excluded: the login screens, the
    # confirm box, the hidden on/off switches of optional functions, and the tick boxes of the indicator table
    # (their column headers and rows carry the icons).
    COVER = """() => {
      const skip = new Set(['login-username','login-password','forgot-username','reset-token','reset-new-password','global-confirm-input']);
      const bad = [];
      document.querySelectorAll('input, select, textarea').forEach(el => {
        if (skip.has(el.id) || el.type === 'hidden') return;
        if (el.closest('.hubx-trow')) return;
        if (el.closest('.hub-multi')) {   // a tick in a "Conditions (tick 1 or more)" group: the group's label has the i
          const g = el.closest('.hub-multi');
          if (g.previousElementSibling && g.previousElementSibling.querySelector('.info-i')) return;
        }
        if (el.closest('[data-fid]') && el.closest('[data-fid]').style.display === 'none') return;
        const hasIcon = n => !!n && (n.classList.contains('info-i') || !!n.querySelector('.info-i'));
        const prev = el.previousElementSibling, next = el.nextElementSibling;
        let ok = false;
        if (prev && prev.tagName === 'LABEL' && hasIcon(prev)) ok = true;
        if (!ok && next && next.tagName === 'LABEL' && (hasIcon(next) || hasIcon(next.nextElementSibling))) ok = true;
        if (!ok && el.id === 'hub-pair-select' && hasIcon(prev)) ok = true;
        if (!ok) bad.push(el.id || el.outerHTML.slice(0, 60));
      });
      return bad;
    }"""
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; warns = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: warns.append(m.text) if "No help text" in m.text else None)
        page.goto(base); page.wait_for_timeout(600)
        # ---- the HELP table itself
        info = page.evaluate("""() => ({ n: Object.keys(HELP).length,
          badLen: Object.entries(HELP).filter(([k,v]) => v.length !== 6 || !v[0] || !v[1]).map(([k]) => k) })""")
        check("every help entry has a title and a first line", info["n"] > 100 and not info["badLen"], str(info))
        # ---- the cover check itself must catch a missing icon (not pass by accident)
        page.click("#maintab-hub"); page.wait_for_timeout(200)
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(300)
        page.evaluate("document.querySelector('#hub_name').previousElementSibling.querySelector('.info-i').remove()")
        check("self-test: the cover check flags a field whose i was removed", page.evaluate(COVER) == ["hub_name"], str(page.evaluate(COVER)))
        page.reload(); page.wait_for_timeout(600)
        # ---- Strategy Hub
        page.click("#maintab-hub"); page.wait_for_timeout(300)
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(300)
        n_all = page.evaluate("document.querySelectorAll('input, select').length")
        check("self-test: the cover check looks at 100+ fields", n_all > 100, str(n_all))
        bad = page.evaluate(COVER)
        check("Hub top + all 8 functions + Add account/pair forms: every field has an i", not bad, str(bad))
        keys = page.evaluate("[...document.querySelectorAll('.info-i')].map(e => e.dataset.help)")
        missing = [k for k in keys if k not in page.evaluate("Object.keys(HELP)")]
        check("every i points to a real text", keys and not missing, str(missing))
        check("no 'No help text' warning in the console", not warns, str(warns[:3]))
        check("16 function titles + 16 menu rows each have an i",
              page.evaluate("document.querySelectorAll('.hubx-title .info-i').length") == 16
              and page.evaluate("document.querySelectorAll('.hubx-iname .info-i').length") == 16)
        check("indicator table: 11 indicator rows + 3 column heads (ENTRY, FLIP, CTREND) have an i",
              page.evaluate("document.querySelectorAll('.hubx-trow:not(.hubx-thead) .info-i').length") == 11
              and page.evaluate("document.querySelectorAll('.hubx-thead .info-i').length") == 3)
        check("every indicator number box (MA, ADX, CI, RSI, MACD, Volume, ATR, Structure) has an i",
              page.evaluate("[...document.querySelectorAll('.hubx-block .hubx-row')].every(r => r.querySelector('.info-i'))"))
        # ---- click: the pop-up
        pick_before = page.evaluate("[...document.querySelectorAll('.hubx-item[aria-pressed=true]')].map(e => e.dataset.fn)")
        page.click('.hubx-item[data-fn="maxloss"] .info-i'); page.wait_for_timeout(150)
        check("click an i in the menu: pop-up opens", page.is_visible("#dlg-help") and "Max Loss per Trade" in page.inner_text("#help-body"))
        check("pop-up shows What it is / What it does", "WHAT IT IS" in page.inner_text("#help-body").upper() and "WHAT IT DOES" in page.inner_text("#help-body").upper())
        page.keyboard.press("Escape"); page.wait_for_timeout(100)
        check("Esc closes the pop-up", not page.is_visible("#dlg-help"))
        pick_after = page.evaluate("[...document.querySelectorAll('.hubx-item[aria-pressed=true]')].map(e => e.dataset.fn)")
        check("clicking an i does NOT pick or unpick the menu row", pick_before == pick_after, f"{pick_before} -> {pick_after}")
        page.click('.hubx-item[data-fn="maxloss"]'); page.wait_for_timeout(100)
        check("clicking the row itself still works", page.evaluate("[...document.querySelectorAll('.hubx-item[aria-pressed=true]')].map(e => e.dataset.fn)") != pick_before)
        page.click('.hubx-item[data-fn="maxloss"]'); page.wait_for_timeout(100)
        page.click('.hubx-item[data-fn="shadow"]'); page.wait_for_timeout(100)
        page.click("#hub-confirm-btn"); page.wait_for_timeout(300)
        page.click('.hubx-item[data-fn="size"]'); page.wait_for_timeout(150)
        page.click('#hub_size_method input[value="Allocation % of balance"]'); page.wait_for_timeout(150)   # tick one method (2026-10-25)
        page.click('#hubpane_size [data-fid="leverage"] .info-i'); page.wait_for_timeout(150)
        txt = page.inner_text("#help-body")
        check("Leverage pop-up has its own text and an example", "Leverage" in txt and "10x" in txt, txt[:120])
        page.click("#dlg-help button"); page.wait_for_timeout(100)
        check("Close button closes the pop-up", not page.is_visible("#dlg-help"))
        page.click('#hubpane_size [data-fid="leverage"] .info-i'); page.wait_for_timeout(100)
        page.mouse.click(5, 5); page.wait_for_timeout(100)
        check("a click outside closes the pop-up", not page.is_visible("#dlg-help"))
        page.focus('#hubpane_size [data-fid="leverage"] .info-i'); page.keyboard.press("Enter"); page.wait_for_timeout(100)
        check("keyboard: Enter on an i opens the pop-up", page.is_visible("#dlg-help"))
        page.keyboard.press("Escape")
        # ---- Add account / Add pair dialogs, Default Settings, other tabs
        for dlg, want in (("dlg-account", 7), ("dlg-okx-account", 9)):
            page.evaluate(f"document.getElementById('{dlg}').showModal()"); page.wait_for_timeout(100)
            n = page.evaluate(f"document.querySelectorAll('#{dlg} .info-i').length")
            page.click(f"#{dlg} .info-i"); page.wait_for_timeout(100)
            on_top = page.is_visible("#dlg-help")
            page.keyboard.press("Escape"); page.wait_for_timeout(100)
            still_open = page.evaluate(f"document.getElementById('{dlg}').open")
            check(f"{dlg}: {n} icons; the pop-up opens on top and Esc keeps the form open", n == want and on_top and still_open, f"{n} {on_top} {still_open}")
            page.evaluate(f"document.getElementById('{dlg}').close()")
        page.evaluate("buildPairFields(null)"); page.evaluate("buildPairFields(null, 'okx-pair-fields', 'okx')")
        for dlg in ("dlg-pair", "dlg-okx-pair"):
            page.evaluate(f"document.getElementById('{dlg}').showModal()"); page.wait_for_timeout(100)
            n = page.evaluate(f"document.querySelectorAll('#{dlg} .info-i').length")
            check(f"{dlg}: symbol + timeframe + isolated + poll + telegram + stale feed all have an i", n >= 6, str(n))
            page.evaluate(f"document.getElementById('{dlg}').close()")
        check("Add pair forms: every field has an i", not page.evaluate(COVER), str(page.evaluate(COVER)))
        page.click("#maintab-defaults"); page.wait_for_timeout(500)
        check("Default Settings: every field has an i (both platforms)", not page.evaluate(COVER), str(page.evaluate(COVER)))
        n_ds = page.evaluate("document.querySelectorAll('#tabpage-defaults .info-i').length")
        check("Default Settings: icons on live switch, email, Binance and OKX fields", n_ds >= 2 + 5 + 6, str(n_ds))
        for tab in ("ledger", "tracker", "analysis"):
            page.click(f"#maintab-{tab}"); page.wait_for_timeout(150)
        check("Analysis, Ledger, Tracker fields have an i", not page.evaluate(COVER), str(page.evaluate(COVER)))
        # 2026-10-24: the Analysis tab now replays the pair's Strategy Hub plan
        an_txt = page.evaluate("JSON.stringify([HELP['an.pair'], HELP['wf.folds'], HELP['mc.sims']])")
        check("Analysis pop-ups tell that it tests the Strategy Hub plan (no Base V3)", "Strategy Hub plan" in an_txt and "Base V3" not in an_txt, an_txt[:300])
        # ---- phone
        page.click("#maintab-hub"); page.wait_for_timeout(150)
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(200)
        size = page.evaluate("Math.min(...[...document.querySelectorAll('.info-i')].map(e => e.getBoundingClientRect().width).filter(w => w > 0))")
        check("phone: the i is big enough to tap", size >= 24, str(size))
        page.click('#hubpane_size [data-fid="size_method"] .info-i') if page.is_visible('#hubpane_size [data-fid="size_method"] .info-i') else page.evaluate("showHelp('size_method')")
        page.wait_for_timeout(150)
        r = page.evaluate("(() => { const r = document.getElementById('dlg-help').getBoundingClientRect(); return [r.left, r.right, window.innerWidth, document.documentElement.scrollWidth]; })()")
        check("phone: the longest-style pop-up fits the screen, no sideways scroll", r[0] >= 0 and r[1] <= r[2] and r[3] <= r[2] + 1, str(r))
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1500, "height": 900})
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
