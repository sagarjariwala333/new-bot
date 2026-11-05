"""
browser_menu_check.py - REAL-browser check of the Save box "Change an open trade" (2026-11-05). OLD DOCSTRING:
browser_presets_check.py - OPTIONAL real-browser check of the PRESETS (2026-10-28): the Presets button on the Strategy Hub,
Load of "HMA Flip", "MACD Cross", "V3", "RSI MA Cross", "EMA Cross" and "MA 1 over MA 2" (rule choices only, NO NUMBERS), nothing left over from the other preset,
a preset cannot be changed, and a preset with arbitrary numbers keyed in is accepted by the real library route
(Playwright + Chromium). Backend = the REAL plan and library routes of app/api; no exchange calls.
Run:  python tests/browser_presets_check.py     (arbitrary numbers only, NOT the owner's settings)
"""
import asyncio, json, os, re, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DATA_DIR = Path("/tmp/hub_bot_browser_menu")
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


from app import hub_menu_layout  # noqa: E402

STATE = {"status": None, "puts": [], "answer": None, "menu": {"order": [], "names": {}, "descs": {}}, "saved": []}


class Body:
    def __init__(self, d): self.__dict__.update(d)


def route(method, path, payload):
    try:
        if path == "/api/auth/status": return 200, {"ok": True}
        if path == "/api/accounts": return 200, api.list_accounts()
        if path == "/api/okx/accounts": return 200, okx_api.list_okx_accounts()
        if path in ("/api/status", "/api/okx/status"): return 200, {"instances": [], "startup_failures": {}}
        if path.startswith(f"/api/status/{acc.id}/"):
            return (200, {"status": STATE["status"]}) if STATE["status"] else (404, {"detail": "instance not running"})
        if path == "/api/hub-menu":
            if method == "GET": return 200, STATE["menu"]
            try:
                STATE["menu"] = hub_menu_layout._clean(payload); STATE["saved"].append(payload); return 200, STATE["menu"]
            except hub_menu_layout.MenuLayoutError as e:
                return 400, {"detail": str(e)}
        if path == "/healthz": return 200, {"status": "ok", "singleton_lock_conflict": None}
        if path == "/api/default-settings": return 200, api.get_default_settings()
        if path == "/api/library" and method == "GET": return 200, asyncio.run(api.get_library())
        if path == "/api/library" and method == "POST": return 200, asyncio.run(api.save_to_library(Body(payload)))
        m = re.fullmatch(rf"/api/accounts/{acc.id}/pairs/([A-Z]+)(/plan|/start)?", path)
        if m:
            sym, tail = m.group(1), m.group(2) or ""
            if tail == "/plan" and method == "GET": return 200, asyncio.run(api.get_pair_plan(acc.id, sym))
            if tail == "/plan" and method == "PUT":
                STATE["puts"].append(payload)
                return 200, {"amend_result": STATE["answer"]}
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



def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    def wait(ms=200): page.wait_for_timeout(ms)
    def names(): return page.eval_on_selector_all("#hub-menu button .hubx-iname", "els => els.map(e => e.childNodes[0].textContent)")
    def descs(): return page.eval_on_selector_all("#hub-menu button .hubx-idesc", "els => els.map(e => e.textContent)")
    def rowvals(): return page.eval_on_selector_all("#hub-menu-rows .hubm-row", "els => els.map(e => [e.dataset.id, e.querySelector('.hubm-name').value, e.querySelector('.hubm-desc').value, e.querySelector('.hubm-pos').value])")
    def open_hub():
        page.click("#maintab-hub"); wait(300)
        page.select_option("#hub-acct-select", index=1); wait(300)
        page.click("#hub-confirm-btn"); wait(500)
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); wait(600)
        open_hub()
        orig_names, orig_descs = names(), descs()
        check("original menu: 18 functions, Pair first", len(orig_names) == 18 and orig_names[0] == "Pair", str(orig_names[:3]))
        page.click("#hub-menu-edit-btn"); wait(300)
        rows = rowvals()
        check("edit window lists every function in menu order with positions 1..18",
              len(rows) == 18 and [r[3] for r in rows] == [str(i) for i in range(1, 19)] and rows[0][0] == "pair", str(rows[:2]))
        # rename + new text + move Entry size (8th) to position 2 by typing the number
        page.fill('#hub-menu-rows .hubm-row[data-id="size"] .hubm-name', "Sizing"); 
        page.fill('#hub-menu-rows .hubm-row[data-id="size"] .hubm-desc', "How big I go")
        page.fill('#hub-menu-rows .hubm-row[data-id="size"] .hubm-pos', "2"); page.press('#hub-menu-rows .hubm-row[data-id="size"] .hubm-pos', "Enter"); wait(200)
        rows = rowvals()
        check("typing a position moves the row there and keeps the typed text", rows[1][0] == "size" and rows[1][1] == "Sizing" and rows[1][2] == "How big I go", str(rows[:3]))
        check("positions are renumbered 1..18", [r[3] for r in rows] == [str(i) for i in range(1, 19)])
        # arrows
        page.click('#hub-menu-rows .hubm-row[data-id="size"] button[title="Move down"]'); wait(150)
        check("the down arrow moves it one place", rowvals()[2][0] == "size")
        page.click('#hub-menu-rows .hubm-row[data-id="size"] button[title="Move up"]'); wait(150)
        check("the up arrow moves it back", rowvals()[1][0] == "size")
        STATE["saved"].clear()
        page.click("#hub-menu-save"); wait(600)
        sent = STATE["saved"][-1] if STATE["saved"] else {}
        check("saved on the server: full order, only the changed texts",
              sent.get("order", [])[:3] == ["pair", "size", "chart"] and len(sent.get("order", [])) == 18
              and sent.get("names") == {"size": "Sizing"} and sent.get("descs") == {"size": "How big I go"}, str(sent))
        n = names(); d = descs()
        check("the menu follows: new order, new name, new grey text", n[1] == "Sizing" and d[1] == "How big I go" and n[0] == "Pair" and n[2] == "Chart type", str(n[:4]))
        # the open function title
        page.click('#hub-menu button[data-fn="size"]'); wait(200)
        check("a function can still be opened from its new place", page.is_visible("#hubpane_size"))
        check("its step counter follows the new place (Function 2 of N)", page.inner_text("#hubstep_size").startswith("Function 2 of"), page.inner_text("#hubstep_size"))
        check("its top shows the new name and text", page.inner_text("#hubtitle_size") == "Sizing" and page.inner_text("#hubdesc_size") == "How big I go")
        # reload: the same from the server
        page.reload(); wait(600); open_hub()
        n = names()
        check("after a reload (same server = another device) the menu is the same", n[1] == "Sizing" and len(n) == 18, str(n[:3]))
        # empty box = original text; original button
        page.click("#hub-menu-edit-btn"); wait(300)
        page.fill('#hub-menu-rows .hubm-row[data-id="size"] .hubm-desc', "")
        page.click("#hub-menu-save"); wait(500)
        check("an empty text box gives the original text back", descs()[1] == orig_descs[7] and names()[1] == "Sizing", descs()[1])
        page.click("#hub-menu-edit-btn"); wait(300)
        page.click('#hub-menu-rows .hubm-row[data-id="size"] button[title="Back to the original name and text"]'); wait(150)
        check("the row's back-to-original button restores its name", rowvals()[1][1] == "Entry size")
        page.click("#hub-menu-cancel"); wait(150)
        check("Cancel changes nothing", names()[1] == "Sizing")
        page.click("#hub-menu-edit-btn"); wait(300)
        page.click("#hub-menu-reset"); wait(150)
        check("'Back to the original menu' puts the original order and text in the window (not saved yet)",
              rowvals()[1][0] == "chart" and names()[1] == "Sizing")
        page.click("#hub-menu-save"); wait(500)
        check("saved: the menu is the original again", names() == orig_names and descs() == orig_descs)
        check("and the server holds nothing changed", STATE["menu"] == {"order": [x for x in STATE["menu"]["order"]], "names": {}, "descs": {}})
        # server refuses: a message, the window stays open
        page.click("#hub-menu-edit-btn"); wait(300)
        page.fill('#hub-menu-rows .hubm-row[data-id="pair"] .hubm-name', "x" * 60)
        page.evaluate("() => { document.querySelector('#hub-menu-rows .hubm-row[data-id=\"pair\"] .hubm-name').maxLength = 500; }")
        page.fill('#hub-menu-rows .hubm-row[data-id="pair"] .hubm-name', "y" * 70)
        page.click("#hub-menu-save"); wait(500)
        check("a refused save shows the reason and keeps the window open", page.is_visible("#dlg-hub-menu") and "Not saved" in page.inner_text("#hub-menu-msg"), page.inner_text("#hub-menu-msg"))
        check("and the menu is not changed", names() == orig_names)
        page.click("#hub-menu-cancel"); wait(100)
        # unknown / new functions: a saved order with an unknown id and a missing id still shows all 18
        STATE["menu"] = {"order": ["tp", "ghost"], "names": {}, "descs": {}}
        page.reload(); wait(600); open_hub()
        n = names()
        check("an unknown id in the saved order is ignored; functions missing from it follow at the end",
              len(n) == 18 and n[0] == "Take profit" and n[1] == "Pair", str(n[:3]))
        check("no JavaScript errors", not errors, str(errors))
        b.close()
    server.shutdown()
    print(f"{sum(res)}/{len(res)} passed")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
