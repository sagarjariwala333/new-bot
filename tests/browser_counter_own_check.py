"""Real-browser check of the Counter-trend section (its own moving average). Run: python tests/browser_counter_own_check.py"""
import sys, threading, json
from http.server import HTTPServer
sys.path.insert(0, ".")
import tests.browser_hub_check as bh


def main():
    from playwright.sync_api import sync_playwright
    server = HTTPServer(("127.0.0.1", 0), bh.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    res = []
    def check(n, c, d=""):
        res.append(bool(c)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/google-chrome") if False else p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 900})
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base); page.wait_for_timeout(600)
        page.click("#maintab-hub"); page.wait_for_timeout(300)
        page.select_option("#hub-acct-select", index=1); page.wait_for_timeout(300)
        page.click("#hub-confirm-btn"); page.wait_for_timeout(200)
        page.select_option("#hub-pair-select", index=1); page.wait_for_timeout(500)
        page.click('.hubx-item[data-fn="size"]'); page.wait_for_timeout(150)
        vis = lambda i: page.is_visible("#hub_" + i)
        check("counter boxes hidden while Counter-trend size is off", not any(vis(i) for i in ("counter_by", "counter_ma_type", "counter_ma_length", "counter_ma_modes")))
        page.check("#hub_counter_on"); page.wait_for_timeout(150)
        check("Counter trend decided by shows once ticked", vis("counter_by"))
        opts = page.eval_on_selector_all("#hub_counter_by option", "o=>o.map(x=>x.value)")
        check("list has own MA first, legacy EMA, and every indicator", opts[:3] == ["", "own", "EMA"] and set(["ma1","ma2","mx","adx","rsi","macd","candle","st","bb","eb","sar"]) <= set(opts), str(opts))
        check("own MA boxes are hidden until Its own moving average is chosen", not vis("counter_ma_type"))
        page.select_option("#hub_counter_by", "own"); page.wait_for_timeout(150)
        check("own MA boxes show", vis("counter_ma_type") and vis("counter_ma_length") and vis("counter_ma_modes"))
        check("type list is SMA EMA WMA HMA ALMA", page.eval_on_selector_all("#hub_counter_ma_type option", "o=>o.map(x=>x.value)")[1:] == ["SMA","EMA","WMA","HMA","ALMA"])
        check("both conditions offered", len(page.query_selector_all("#hub_counter_ma_modes input[type=checkbox]")) == 2)
        check("ALMA boxes hidden for EMA/blank type", not vis("counter_alma_offset"))
        page.select_option("#hub_counter_ma_type", "ALMA"); page.wait_for_timeout(120)
        check("ALMA boxes show for ALMA", vis("counter_alma_offset") and vis("counter_alma_sigma"))
        page.select_option("#hub_counter_ma_type", "HMA"); page.wait_for_timeout(120)
        check("ALMA boxes hide again", not vis("counter_alma_offset"))
        check("legacy EMA box hidden", not vis("counter_ema"))
        page.select_option("#hub_counter_by", "EMA"); page.wait_for_timeout(120)
        check("legacy EMA box shows only for EMA choice, own boxes hide", vis("counter_ema") and not vis("counter_ma_type"))
        page.select_option("#hub_counter_by", "ma1"); page.wait_for_timeout(120)
        check("indicator choice hides own boxes", not vis("counter_ma_type") and not vis("counter_ema"))
        check("choosing MA 1 ticks CTREND on MA 1 in Indicators", page.evaluate("document.querySelector('#hub-panes .hub-ct[data-ct=ma1]').checked"))
        page.select_option("#hub_counter_by", "own"); page.wait_for_timeout(120)
        check("choosing own unticks every CTREND box", page.evaluate("[...document.querySelectorAll('#hub-panes .hub-ct')].every(x=>!x.checked)"))
        line = page.inner_text("#hub_counter_line")
        check("info line asks for the MA numbers", "key in" in line, line)
        page.select_option("#hub_counter_ma_type", "EMA"); page.fill("#hub_counter_ma_length", "200")
        for x in page.query_selector_all("#hub_counter_ma_modes input[type=checkbox]"):
            if x.get_attribute("value").startswith("Price"): x.click()
        page.wait_for_timeout(150)
        line = page.inner_text("#hub_counter_line")
        check("info line shows the own MA", "EMA 200" in line and "Price above / below MA" in line, line)
        # CTREND tick in Indicators then untick -> back to own
        page.click('.hubx-item[data-fn="ind"]'); page.wait_for_timeout(150)
        page.check("#hub-panes .hub-ct[data-ct=rsi]"); page.wait_for_timeout(100)
        check("CTREND tick sets the list to RSI", page.input_value("#hub_counter_by") == "rsi")
        page.uncheck("#hub-panes .hub-ct[data-ct=rsi]"); page.wait_for_timeout(100)
        check("CTREND untick goes back to own MA", page.input_value("#hub_counter_by") == "own")
        # plan built
        plan = page.evaluate("hubBuildPlan()")
        c = plan["sizing"]["counter"]
        check("plan carries own MA", c["by"] == "own" and c["ma_type"] == "EMA" and c["ma_length"] == 200 and c["ma_modes"] == ["Price above / below MA"] and "ema_length" not in c, json.dumps(c))
        # preset V3
        page.evaluate("hubApplyPreset(HUB_PRESETS.find(x=>x.id==='v3'))"); page.wait_for_timeout(200)
        page.click('.hubx-item[data-fn="size"]'); page.wait_for_timeout(150)
        check("V3 preset: own MA, EMA, price above/below, length empty", page.input_value("#hub_counter_by") == "own" and page.input_value("#hub_counter_ma_type") == "EMA" and page.input_value("#hub_counter_ma_length") == "" and page.evaluate("hubValue(hubField('counter_ma_modes'))")[0].startswith("Price"))
        # a plan round trip through hubFill
        plan2 = {"sizing": {"counter": {"on": True, "by": "own", "ma_type": "WMA", "ma_length": 55, "ma_modes": ["MA rising / falling", "Price above / below MA"], "alloc_pct": 3, "leverage": 2}}}
        page.evaluate("p => hubFill(p)", plan2); page.wait_for_timeout(150)
        check("hubFill shows saved own MA", page.input_value("#hub_counter_ma_type") == "WMA" and page.input_value("#hub_counter_ma_length") == "55" and len(page.evaluate("hubValue(hubField('counter_ma_modes'))")) == 2)
        old = {"sizing": {"counter": {"on": True, "by": "EMA", "ema_length": 120, "alloc_pct": 3, "leverage": 2}}}
        page.evaluate("p => hubFill(p)", old); page.wait_for_timeout(150)
        check("old EMA plan loads as legacy EMA with its length", page.input_value("#hub_counter_by") == "EMA" and page.input_value("#hub_counter_ema") == "120")
        lines = page.evaluate("hubSummaryLines(hubBuildPlan()).join('\\n')")
        check("summary text says chart close vs EMA 120", "EMA 120" in lines, lines[-300:])
        page.evaluate("p => hubFill(p)", plan2); page.wait_for_timeout(100)
        lines = page.evaluate("hubSummaryLines(hubBuildPlan()).join('\\n')")
        check("summary text says its own WMA 55", "its own WMA 55" in lines, lines[-300:])
        check("no page errors", not errors, str(errors))
        b.close()
    print(f"\n{sum(res)} passed, {len(res)-sum(res)} failed"); sys.exit(0 if all(res) else 1)

if __name__ == "__main__":
    main()
