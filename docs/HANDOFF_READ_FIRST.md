# Strategy Hub - handoff to a new chat (2026-10-20, clean hub build strategy_hub_bot)

This note is inside the zip (docs/HANDOFF_READ_FIRST.md). Give the new chat the zip only.

## Owner rules (AskMe)
- Very short replies, simple English, use his exact words.
- Do checks, rechecks and final checks before results, especially code.
- DO NOT FOMO: show, discuss, get approval first. No unapproved changes.
- Nothing goes live until he says. There is no For Dev build.
- Deliver the FULL zip only, file name with date + time (SGT).

## Rules built in
- Flip = "CLOSE, FLAT, CHECK FOR CHANGES IN RULES, ELSE OPEN OPPOSITE IF NO CHANGE." Candle close gives the signal.
- No default strategy: he picks pair, timeframe, chart type (Candles / Heikin Ashi / Renko), entry and flip/exit indicators.
- Each indicator in its own box with ON/OFF for ENTRY and FLIP/EXIT. OFF indicators need no numbers.
- Entry wait (0 = at once). No stacking. Size = allocation % of available balance.
- Max loss per trade (his study 6%). No ATR stop.
- TP: ATR, 3 levels, next limit placed only after the previous is hit.
- Trailing SL: extreme low/high of X candles, only tightens.
- Candle colour only for Heikin Ashi / Renko (up candle = long, down = short). Refused on plain Candles.
- Signals come from HA/Renko, trades go on normal candles. ADX, CI, Volume, stop, TP ATR, trailing read NORMAL candles (his decision).
- Dashboard: "Strategy Hub" tab, boxes in trade-flow order 1-9 (Pair & chart, Indicators, Combine & flip, Entry wait, Entry size, Max loss, Trailing stop, Take profit, Shadow mode), responsive (5 / 2-3 / 1 across).

## State
- 738 tests pass; browser checks pass (Hub tab 54, Default Settings 30). (2026-10-23)
- A pair with no saved plan does NOT start (app/default_settings.require_pair_ready). Make a pair with Add pair, then give it a plan in the Hub tab.

## Not done / not proven
- TP limit orders and trailing-stop moves NOT proven on a real exchange: use Binance testnet / OKX demo first.
- A TP fill while the bot is down is logged but may miss the ledger.
- DONE 2026-10-24: "Remove plan from pair" button (see Strategy Library).
- Pine for the hub not yet compiled in TradingView.
- My Renko is built from closed candles: close to TradingView, not identical.
- Candle that closes exactly at its open = neither long nor short.

## Latest decisions (clean hub build)
- The Hub is a NEW product. Old Hull is not a mode. Every function is standalone: nothing runs unless the owner ticks it. NO preset strategy.
- No starter values: first install fully blank. A pair with no plan will not start.
- Hub box 5 = ENTRY SIZE (allocation % + leverage). Box 6 = STOP LOSS (max loss per trade + trailing stop, one box). Box 7 = TAKE PROFIT.
- Trailing moves at every candle close, only tightens (new stop first, old one off after).
- Owner decision (12:02 SGT): the old Hull fields are REMOVED from the setup page and Add pair form. Setup page = pair-level settings only (timeframe, isolated margin, poll seconds, Telegram, stale-feed seconds, OKX trigger type). Old Hull code is still in the files, unreachable; deleting it is a separate step, only if he asks.
- BUILT (box 5 and box 8): trend vs counter-trend sizing (inside ENTRY SIZE) and SHADOW MODE (paper trades after a new tracker high; they do not simulate TP or trailing). Both OFF unless ticked; plans saved before have them OFF.
- NOT PROVEN on a real exchange: TP limit orders, trailing moves, and the whole shadow flow on live data (use testnet / demo first).
- Name: strategy_hub_bot. Order-id prefixes hullbot_ / hullbotokx unchanged on purpose.
- Owner is easily angered by over-asking and by scope growth: do exactly what he says, in his words, and show first for anything new.

## Added 2026-10-23 (owner decisions, exact words)
- ENTRY SIZE has a method choice: "Allocation % of balance" (as before) or "Fixed base amount (grows with the last closed total)".
- Fixed base: the amount is margin in USDT, keyed in by the owner; leverage is its own box (position = amount x leverage). Start with the base, which is the minimum. The next entry is based on the TOTAL of the last closed trade (entry + result), all fills added up (TPs, SL, trailing SL, BE). Compounding, no reset, no cap, no extra balance rule written by us.
- Trend and counter-trend each have their own base amount, editable; ONE running total for both ("ALL AS 1").
- MAX LOSS is its own box (own section 6) with a choice of basis: % of available balance, % of entry amount, fixed $ amount. Trailing stop is its own box (section 7).
- Paper (shadow) trades never change the running total. With the fixed base, a paper entry needs the available balance (turned into a % of the wallet); if it cannot be read, that paper entry is skipped and logged.
- The running total (last_total) is kept in the pair's hub state file and survives a restart.
- Known small limit: when take-profit fills close a whole trade, the entry commission is not in the ledger (already so before), so that trade's total is a little high by the entry fee.
- A plan-hash change on upgrade restarts a pending entry-wait count once: deploy when no entry wait is pending.
- Owner is easily angered by over-asking: do exactly what he says, in his words, no extras.

## Added 2026-10-23 later (Layout 2 build, owner said "go")
- Hub screen is Layout 2 (chosen by the owner): strategy name + account on top; menu of 8 functions on the left; boxes of the open function on the right, each with a short explanation; Save and Execute. Core functions (Pair & chart, Indicators, Combine & flip + hold rule, Entry) are always picked; Max loss, Stop loss (trailing), Take profit and Shadow mode are picked or not (not picked = that switch is saved OFF). Execute = save + start the pair after a confirm. A pair must already exist on the account (added in the Binance or OKX tab; the first tab was renamed from Trading to Binance).
- Combine has ALL / ANY / "At least N" (plan keys entry_min / flip_min; N between 1 and the number of indicators ticked on that side).
- Entry: "Candles to wait" default is blank/0; above 0 a pop-up says "Delay in entry up to N candles".
- Lock-out % (plan key sizing.lock_pct, fixed base only, absent = off): next entry = previous entry + that % of the trade's profit; loss in full; base is still the minimum. hub.carry_total() does the math.
- "Email alert on margin" (sizing.margin_alert): when an entry needs more margin than the available balance, email + Telegram, once per candle. Meaning chosen by the builder: this entry's margin vs the available balance right now (other pairs have already taken part of it). Tell the owner if he meant something earlier / different.
- Strategy name = plan.name (label only; not part of the rules hash).
- The completed plan gained inert keys (lock_pct, margin_alert, entry_min, flip_min): on upgrade the plan hash changes once, so a pending entry-wait count restarts once. Deploy when none is pending.
- Tests: tests/test_hub_new_boxes.py (33 tests), tests/browser_hub_check.py (real browser, 85 checks, Layout 2).

## Added 2026-10-23 later still (info icons, owner said "move forward")
- An "i" icon sits beside every field, tick box and choice (Hub: top, 8 functions, boxes, indicator table and numbers; Default Settings; Add pair; Add Binance / OKX account; Ledger, Tracker, Analysis). Click it: a pop-up with the full explanation (What it is, What it does, Choices / ON and OFF, Example, Careful). Closes with Close, Esc or a click outside.
- All texts live in one place: the HELP table in app/static/dashboard.html (125 entries). Dynamic forms call ih('key'); fixed HTML uses <span data-ih="key"></span>.
- tests/browser_info_icons_check.py (real browser, 30 checks) fails if any field has no icon.
- The Analysis tab (Walk-Forward, Monte Carlo) replays the OLD Base V3 rules, not the Hub plan. Its pop-ups say so. Not changed.
- In Combine, Choppiness / Volume / ATR say OK for both sides: with ANY they can cancel a signal. The pop-up says so. Not changed.

## Added 2026-10-23 afternoon (stop ladder build, owner said "update what's been cleared first")
Menu is now 9 functions: Pair & chart, Indicators, Combine & flip + hold rule, Entry, Max loss, Stop Loss, Trailing Stop, Take profit, Shadow mode.
- ONE STANDARD for distances (Stop Loss, Trailing Stop Step, Take Profit): "% of ATR" (100 = 1 ATR, 200 = 2 ATR; ATR of normal candles at the entry candle, fixed for the trade) or "% of price movement" (3 = 3%, 0.5 = 0.5% of the entry price; leverage plays no part).
- STOP LOSS (new, optional): the normal stop of the trade, from the entry price.
- TRAILING STOP (renamed from "Stop loss (trailing)"), one type: Step (start "Same as Stop Loss" or "Own value"; gap; moves LIVE), Lowest low / highest high (N candles), Moving average (MA 1 or MA 2; takes over only on the safe side of the price). Only tightens. Replaces the Stop Loss once it takes charge; if no trailer, the Stop Loss stays.
- MAX LOSS = the last line of defense (safety net if the normal stop fails).
- THE LADDER (owner design): the NEAREST level rests on the exchange - Stop Loss / Trailing Stop as stop-LIMIT (trigger = limit, option B), Max Loss as stop-MARKET. The levels behind it are in the bot's memory (hub state, survives a restart); the bot watches the live price about every second and closes AT MARKET if the price goes through one while the trade is still open. Flip stays active: whichever comes first closes the trade.
- Stop-limit cannot use close-all on Binance: it carries a quantity (reduce-only) and is put back with the remaining quantity after every take-profit fill.
- If the exchange refuses a stop-limit and there is no stop resting, the Max Loss stop-market goes on instead (Telegram note); with no Max Loss the position is protected-or-flattened as before (never left naked).
- TAKE PROFIT: "Measured on" for all 3 targets; "ATR multiple" became "Distance (%)" (old plans converted, 1.5 -> 150). Total up to 100; the rest rides the trend.
- ALLOW MID-TREND RE-ENTRY (Entry page, NOT ticked by default, old plans too): after any close, no new entry on the same side while that entry signal is still on; it waits until the signal has ended. The other side is never blocked; a Reverse flip still opens the opposite side.
- Reminder pop-ups: changing Max loss shows the Stop Loss settings; changing Stop Loss shows the Max loss settings. Whatever is input is accepted.
- New i texts for every new field.
- Tests: tests/test_hub_ladder.py (41), tests/browser_hub_stops_check.py (27 real-browser checks). Full suite 812 OK; browser checks 86 / 30 / 30 / 27.
- NOT PROVEN on a real exchange: stop-limit orders (Binance algo type STOP with quantity + reduceOnly; OKX conditional with slOrdPx + sz), next to reduce-only take-profit orders - Binance may refuse it. Test on Binance testnet / OKX demo first.
- Known limits: on OKX the limit order left by a triggered stop-limit is not cancelled by id (the adapter has no child order id); a refused stop-limit is retried every 60 s; plan hash changes once on upgrade (deploy when no entry wait is pending).

## Added 2026-10-24 (one tab per function, owner said "accept")
- Hub menu is 13 tabs, one function each: Pair, Chart type, Indicators, Combine, On a flip signal, Hold rule, Entry wait, Entry size (always picked, except Hold rule), and Max loss, Stop Loss, Trailing Stop, Take profit, Shadow mode, Hold rule (you pick).
- Hold rule is now a menu pick (its "Use the hold rule" tick box is gone; picked = ON). Same fields, same rules, same plan keys; old saved plans load the same.
- Browser checks updated for the 13 tabs (hub 89, stops 30 incl. the Hold rule pick, icons 30).

## Added 2026-10-24 morning (shadow + Chart tab, owner said "go")
- SHADOW (owner: "Shadow should, as it says, shadow the real trade all the way - EXCEPT that it should NOT go into pause when the actual trade goes to pause"): app/shadow_book.py. While Shadow mode is picked (OFF by default; owner: only trend following needs it, day trade and scalping don't), a PAPER copy of the whole plan runs all the time: entry, wait, combine, flip / hold, re-entry, Stop Loss, Max loss, all 3 Trailing Stop types and partial Take Profit, on the live price (about every second) like the real trade. Never an order. Its paper equity = the shadow tracker (hub state "shadow_book").
- Trades off (rule kept, owner: "it should follow the rules"): a real winning trade that makes a new tracker high starts it; the next N shadow trades are off for real trading ("Number of trades off"); then real trading goes live again. Counted when shadow trades close. A flip-close that ends it blocks the real entry on that same candle (as before).
- The shadow keeps running when real trading is paused: trades off, Stop on the pair ("SHADOW RUNNING" badge; manager.pause -> instance.stop(keep_shadow=True) = shadow-only mode: no real trading at all, an open real trade is left to its resting exchange orders), and STOP ALL. A stopped pair with Shadow mode also runs its shadow after a process restart (start_all_enabled -> start_shadow_only). Start replaces the shadow-only instance with a real one. Saving a plan on a stopped pair refreshes its shadow. Delete / restart / shutdown stop fully.
- The older candle-only paper engine (hub_shadow_step) was removed; the Tracker tab's paper list now shows the shadow's trades (flag during_trades_off).
- CHART tab (read only): pick a pair -> the real exchange candles (pair timeframe) on top with real trade marks (blue), shadow trade marks (orange, hollow) and the open real trade's Stop Loss / Trailing Stop / Max loss / TP lines; the shadow tracker line below, one point per candle, same time line (hover shows both). Updates every 5 s while the tab is open. Route GET /api/chart/{platform}/{account}/{symbol} (app/chart_api.py).
- The chart is drawn by the dashboard itself on two canvases. The plan said TradingView's Lightweight Charts library, but it could not be downloaded here (package registry blocked) and loading it from a public CDN at run time would put an unchecked outside script inside the trading dashboard - so no outside library is used. Drag = move back in time; wheel / + - = zoom; Latest.
- Tests: tests/shadow parts in test_hub_sizing_shadow.py (rewritten for the new shadow), test_chart_and_shadow_only.py, browser_chart_check.py (20). Full suite 822 OK.

## Added 2026-10-24 (Strategy Library, owner said "GO! THIS GONNA BE GREAT")
- Owner: "a strategy if proven good needs to be saved.. to avoid if I forget or overlooked" and "I can look back in the library when I wanna test it with different pairs."
- app/strategy_library.py, file DATA_DIR/strategy_library.json (Railway volume; survives restarts and new zips). Two lists:
  - Saved: "Save to library" button (Hub bottom bar) keeps the strategy on screen under a name, with date + time (SGT), platform, account, pair, timeframe, one-line summary. Only a complete plan (validate_plan). Same name again = a second copy; nothing is overwritten. The pair itself is not changed.
  - History (automatic): every time a pair's plan is changed (hub_api.apply_plan) or removed, the OLD plan is kept with date + time (SGT), pair and "changed" / "removed". Nothing when the plan is the same or the pair had none. A library problem never stops the plan save (logged).
- "Library" button (Hub top): lists Saved and History, newest first, with Details (full summary lines). Load = fills the Hub form (and timeframe) for the pair chosen, after a confirm; nothing is saved until Save / Execute. A timeframe the platform does not have (8h on OKX) is left blank to pick. Delete = only after a confirm.
- Nothing in the library is deleted unless the owner deletes it. No limit, no clean-up. An unreadable library file is never overwritten.
- "Remove plan from pair" (Hub bottom bar, confirm): the plan goes into History first, then the pair has no plan (it will not start; a running shadow ends). Refused while the pair is trading ("Stop the pair first") and while a trade may still be open (status, pending reconciliation, the bot's open-trade record, or the hub stop / TP memory). Frozen pair: refused.
- Routes: GET/POST /api/library, DELETE /api/library/{id}, DELETE /api/accounts/{a}/pairs/{s}/plan and /api/okx/accounts/{a}/pairs/{s}/plan.
- Tests: tests/test_strategy_library.py (23), tests/browser_library_check.py (34). Full suite 845 OK.

## Added 2026-10-24 (Main for ENTRY / FLIP, owner said "Go .. and STOP COMPLICATING THINGS")
- Owner: "there's only ONE and that ONE turning point that confirms the move in a strategy.. the rest makes up as confluence to confirm the move." Example: "main is a go.. but adx is no but rsi is good.. still a no go". "let me choose which is main strategy.. which will be chosen for open and or exit too". "whatever I chose for entry may not be chosen for exit.. it's up to me".
- Indicators tab: "Main for ENTRY" and "Main for FLIP / EXIT" lists (none, or one of the indicators ticked for that side). Plan keys main_entry / main_flip (block key; left out = none = as before).
- Rule (strategy_hub.compute_hub combine): the side's signal = Combine result (ALL / ANY / At least N over all ticked indicators, the Main counted) AND the Main gives that side on the closed candle. SL, ML, TS, TP unchanged. A flip with Reverse opens the opposite side as before.
- validate_plan: the Main must be ticked for its side. Unticking the Main's indicator clears it in the dashboard.
- Tests: tests/test_hub_main.py (11), tests/browser_main_check.py (13). Full suite 856 OK.
- The Hub Pine script does NOT have Main yet (KIV 9).

## Added 2026-10-24 (FLIP rename + wake at the candle close, owner: "Yes do that so it's instant.. else we catch running trend")
- "FLIP / EXIT" is called "FLIP" everywhere (owner: "just call it FLIP"). Name only; plan keys unchanged (ind.*.flip, combine_flip, flip_min, main_flip). A flip = exit the current trade, go flat, check for changes in rules, enter opposite (Reverse); Close only = exit and stay flat.
- Wake at the close (hub_trading CANDLE_MS, _hub_close_wake_in; instance._sleep_until_next_tick): a Hub pair wakes 0.5 s after its candle close instead of at its next poll, so the flip / entry starts at once. If the exchange does not show the closed candle yet, it looks again every second for up to 30 s. The close time = last processed candle open + 2 x candle length (the exchange's own open times). "1M" keeps the poll timing. Poll seconds still apply between closes (live stop checks every second as before).
- Time from the close to the new trade = 0.5 s + the order calls (close, confirm flat, open, stop, TP). NOT measured on a real exchange: check on testnet / demo.
- Tests: tests/test_hub_close_wake.py (6). Full suite 862 OK.

## Added 2026-10-24 (part closed outside the bot, owner: "Fix both")
- When part of an open Hub trade is closed outside the bot (by hand on Binance / OKX), the bot now records that part in the LEDGER (reason "manual_partial_close") and adds its result to the FIXED BASE trade total (hub_trading._hub_outside_partial, called from instance._check_position_size_changes).
- Price and fees from the exchange's own fills (userTrades / OKX fills: close side, after the entry, not the bot's entry or TP orders, never twice - fill ids kept in hub state outside_fill_ids). No fill found = mark price, ledger row confirmed = False and gross.
- Not an outside close: take-profit fills (recorded first), a triggered stop-limit (the stop working). Memory reset at every new entry (outside_closed_qty, outside_fill_ids).
- OKX fills now also carry qty (coins = fillSz x ctVal), time, side (upper case), id - the same names as Binance.
- Unchanged: the Base V3 tracker counts as before (the bot's own share at the final close).
- NOT proven on a real exchange: check on testnet / demo (close part by hand, look at the Ledger tab).
- Tests: tests/test_hub_outside_partial.py (7), one OKX test. Full suite 870 OK.

## Added 2026-10-24 (Trailing Stop "Begins at a price move", owner said "Go.. don't forget the explanation")
- Owner: "the first SL is 2atr.. upon touching 0.5 ATR it goes to BE.. THEN FROM THERE IT TRAILS 0.25" - on 1H he does not want to cash out anything; the whole position rides the trend.
- Trailing Stop, Step type, Start has a 3rd choice "Begins at a price move" with 3 boxes (same Measured on as the trail): "Begins when the price moves (%)" (trail.begin_value), "Stop goes to (%)" (trail.lock_value: 0 = breakeven, plus = profit locked, minus = below the entry), "Then moves every (%)" (trail.gap, same key as the old Gap).
- Rule (strategy_hub.step_setup / step_level, live price): the Stop Loss is in charge until the price has moved Begins in favour; then the stop goes to entry + Stop goes to; then one step for every further step; never back. Stop Loss stays behind it, Max Loss last. Same rule in the shadow.
- Example (entry 100, ATR 10; 50 / 0 / 25): 105 -> 100, 107.5 -> 102.5, 110 -> 105.
- "Stop goes to" must be lower than "Begins" (refused otherwise). The old "Same as Stop Loss" / "Own value" starts are unchanged.
- Tests: tests/test_hub_trail_begins.py (10), tests/browser_trail_begins_check.py (12). Full suite 880 OK.

## Added 2026-10-24 (Analysis tab on the Hub, owner: "Start 3" then "Go")
- Walk-Forward and Monte Carlo now replay the PAIR'S OWN Strategy Hub plan (app/hub_backtest.py), not Base V3. Same engine as the shadow (app/shadow_book.py): Main + confluence, FLIP, entry wait, re-entry, first fresh reversal, hold, SL, Max loss, every Trailing Stop type (incl. Begins), partial TP, entry size (allocation % or fixed base with its running total / lock-out). A pair with no plan is refused (400). Binance and OKX.
- Inside a candle: TradingView's order (up candle open-low-high-close, down candle open-high-low-close). A stop the open already passed fills at the open (gap); otherwise stops / targets fill at their level; a flip at the close.
- Costs: commission % + slippage % on every fill (entry + each exit part) on the traded size (allocation x leverage).
- Trades off: owner did not answer, so the plan's own rule is followed - Shadow mode picked = after a counted winning trade that makes a new high, the next N trades are NOT counted (like the bot); not picked = every trade counts. Shown in a new column "Not counted (trades off)".
- Walk-Forward: one replay over the whole history (first 200 candles warm up), then the tested candles are cut into N windows; each window starts from the starting equity; plus a "Whole period" row. Monte Carlo reshuffles the counted trades' net % results (compounding).
- Binance get_klines now pages backwards (endTime) above 1,500 candles, so the Analysis "candles to pull" up to 5,000 works. Up to 1,500 = one request as before.
- app/analysis.py (Base V3 replay) is kept only for reference and its tests.
- A trade still open at the end of the history is not counted.
- Tests: tests/test_hub_backtest.py (13), OKX endpoint tests updated, tests/browser_analysis_check.py (11). Full suite 894 OK.

## Added 2026-10-25 (RSI / MACD conditions, owner: "add those strategy in these indicators" then "Go")
- From common RSI / MACD strategies (IG, Capital.com, KuCoin guides). Each block got a "Condition" (plan key ind.rsi.mode / ind.macd.mode):
  - RSI "Above / below level" (as before), "Overbought / oversold" (long when RSI crosses back UP above Oversold, short when it crosses back DOWN below Overbought; on for that candle + N), "Divergence" (TradingView style: pivots on RSI with Pivot candles left/right, the two pivots Pivot..Max look-back candles apart; bullish = RSI higher low + price lower low, bearish = RSI lower high + price higher high; signal on the confirm candle = pivot + Pivot candles, never earlier; on + N).
  - MACD "Histogram above / below 0" (as before = MACD line vs signal line), "Zero line" (MACD line above / below 0). "Histogram must also move the trade way" works with both.
- Older plans without a Condition keep what they did (upgrade_plan). Checks: 0 < Oversold < Overbought < 100; Max look-back > Pivot candles.
- Same in the shadow and the Analysis tab (they use compute_hub). The Hub Pine script does NOT have these yet.
- Tests: tests/test_hub_rsi_macd_modes.py (10), tests/browser_rsi_macd_check.py (16). Full suite 904 OK.

## Added 2026-10-25 (more indicator options, owner: "Yes.. also amend the volume strategy", "1 and 5 - have an option checkbox", "Go")
- From common uses found online (guides listed in the chat). All are options he picks; OFF / not chosen = as before:
  - Candle colour: tick "Strong candle only" (ind.candle.strong): long needs no lower wick, short no upper wick (Renko always met).
  - MA 1 / MA 2: Condition "Pullback to MA (bounce)": long = chart low <= MA and close > MA; short = high >= MA and close < MA; on for that candle + N.
  - ADX: tick "ADX must be rising" (ind.adx.rising): ADX above the previous candle's.
  - ATR: tick "ATR must be rising" (ind.atr.rising).
  - Volume: tick "Average without the signal candle" (ind.vol.exclude_current): average of the previous N candles only.
- The 4 ticks are optional keys (strategy_hub.OPTIONAL_KEYS): an older plan without them = OFF.
- Same in the shadow and the Analysis tab. The Hub Pine script does NOT have these yet.
- Tests: tests/test_hub_more_options.py (8), tests/browser_more_options_check.py (13). Full suite 912 OK.

## Added 2026-10-25 (Condition ticks + Counter trend decided by, owner: "Ok go. So we don't have to rebuild it again and again..")
- CONDITION TICKS: MA 1, MA 2, MA Crossover, RSI and MACD: "Conditions (tick 1 or more)" (plan key ind.<b>.modes, a list) instead of one choice; ALL ticked must agree on that indicator (owner: "if I want just 1 ma with two conditions .. I MUST BE ABLE TO HAVE THAT"). One shared N per indicator. A used indicator needs at least 1 tick. Older plans: their one "mode" becomes the ticked one (upgrade_plan).
- COUNTER TREND DECIDED BY (owner: "It has to be flexible", "is a flip an entry but to another direction? True"): Entry size -> "Counter trend decided by" (sizing.counter.by): "EMA" = the EMA length box (as before, default for older plans) or one indicator with a side (MA 1, MA 2, MA Crossover, ADX / DI, RSI, MACD, Candle colour, Structure break). With the trend = that indicator says the same side as the entry with its own conditions; else counter-trend. Works at every entry, a flip too. The indicator does NOT need an ENTRY / FLIP tick, only its numbers (they open in Indicators).
- Indicators table has a CTREND column (one tick only). The CTREND tick and the dropdown are ONE setting: change one, the other follows (dashboard hubCtSync / hubCtTicked). Ticking CTREND turns the second size on. Review line: "AGAINST the trend (decided by MA 1): ...".
- 0 = NO counter-trend entry at all (counter allocation % or counter base amount): live bot drops the entry (a flip into that side closes and stays flat), the shadow and the Analysis tab skip it too (strategy_hub.counter_entry_off). Below 0 refused.
- Choppiness / Volume / ATR cannot decide the counter trend (no side): refused. Candle colour as counter trend needs Heikin Ashi / Renko.
- The Hub Pine script does NOT have these yet.
- Tests: tests/test_hub_ticks_ctrend.py (12), tests/browser_ticks_ctrend_check.py (18); older checks updated for the ticks. Full suite 924 OK.

## Added 2026-10-25 ("Close only" removed, owner: "First let's remove the close. It's complicating things.. then we improve flip tab")
- Owner: "A flip is closing current position. Checks for any changes while in flat. Adopt the change if needed and enter based on the new signal, if no rule change enter as what's the rules, based on the new signal." The flip signal IS the new entry signal.
- strategy_hub.FLIP_ACTIONS has only "Reverse (close + open opposite)"; upgrade_plan turns an old "Close only (go flat)" (or a missing flip_action) into it. Exits without a reverse come only from TP / SL / TS / Max loss.
- Dashboard tab "On a flip signal" has no choice any more: it shows the rule (CLOSE, FLAT, CHECK FOR CHANGES IN RULES, ELSE OPEN OPPOSITE IF NO CHANGE). Owner: the flip tab will be improved next.
- Tests that used "Close only" now check that an old Close only plan flips. Full suite 924 OK.

## Added 2026-10-25 (Flip tab Yes / No + "Indicator Combination", owner: "Default Yes.. also please change the Indicator Combination everywhere else.. then GO!")
- Owner: a flip is "close and open a new direction"; it happens when a new signal comes and a trade of the opposite direction is open. "A flip will never be a signal.. just open or close!"
- Flip tab (plan key flip_rules), each Yes / No, Yes = follows the main settings:
  - Same as entry (same_entry): Yes = the flip uses the ENTRY signal (dir_f = dir_e; FLIP ticks, Main for FLIP, Indicator Combination for FLIP not used and hidden). No = the flip's own rules (FLIP column, Main for FLIP, Indicator Combination for FLIP).
  - Same as hold rule (same_hold): Yes = the Hold rule tab; No = the flip's own hold (hold_on, hold_level) - strategy_hub.effective_hold.
  - Same as entry wait (same_wait): Yes = Entry wait; No = the flip's own wait (wait, 0 = instant) - strategy_hub.flip_wait, wait_step(flip_wait=).
- New plans start with all 3 = Yes. A plan saved before keeps what it did (upgrade_plan: same_entry No, same_hold Yes, same_wait Yes).
- "Combine" is called "Indicator Combination" everywhere (menu, boxes, i texts, messages). Plan keys unchanged (combine_entry / combine_flip).
- Same in the shadow and the Analysis tab. The Hub Pine script does NOT have this yet.
- Tests: tests/test_hub_flip_tab.py (9), tests/browser_flip_tab_check.py (23). Full suite 933 OK.

## Added 2026-10-25 (Hold rule default on, owner: "Make it default on.. as I don't want to be distracted by noise.")
- A new strategy starts with the Hold rule picked in the menu (HUB_FUNCS hold "on: true") and its ADX level at 25 (owner: "So fix the adx too 25"; field init, editable). The ADX / DI numbers in Indicators are still keyed in by the owner. He can unpick it. Saved plans keep what they have.

## Added 2026-10-25 (Entry wait tab names + defaults, owner: "I want default and rename it proper")
- "Candles to wait" -> "Candles Wait", starts at 0 on a new strategy (editable).
- "First trade only on a fresh reversal" -> "Fresh Reversal on First Trade", starts ticked ("this only applies to first trades").
- "Allow Mid-Trend Re-entry" -> "No Mid-Trend Re-entry", starts ticked = no re-entry (the box is the opposite of the plan key allow_reentry: ticked = allow_reentry False; dashboard field "inv"). Owner note kept in its i text: "This is only good for trend trades but if we have TP and SL .. having mid trend may give a second wave of profits.. but on safe side all entry should be fresh".
- Saved plans keep their own values. Engine unchanged.

## Added 2026-10-25 (Entry size tab layout + Next entry line, owner: "Want it. Go")
- 3 sections: Size method (tick one: Allocation % of balance / Fixed base amount; only the ticked method's boxes + Leverage show), Counter-trend size (tick; then Allocation % or Base amount against the trend + Leverage against the trend; 0 = no counter-trend entry), Margin alert.
- Owner: the counter trend "has to follow what I decide the counter trend should be.. here it only decides the entry size". The "Counter trend decided by" dropdown and the EMA length box are gone from the screen (kept hidden, still filled by the CTREND tick, so older EMA plans keep working); a line shows who decides it (e.g. "MA 1 (EMA 200) - CTREND in Indicators"). Ticked without any CTREND = Save stops: "tick CTREND on one indicator in Indicators first".
- "Next entry" line (display only): allocation = % of balance x leverage; fixed = last closed total (never below the base, from the pair's status: hub.last_total, new in _hub_status) x leverage; plus the against-the-trend size or "NO entry (0)".
- Dashboard field types added: "pick1" (tick one), "info" (display line); "hidden" rows. Engine unchanged.
- Tests: tests/browser_entry_size_check.py (21); older checks updated for "tick one". Full suite 933 OK.

## Added 2026-10-26 (rename, owner: "re-word it Max Loss per Trade")
- "Max loss" is called "Max Loss per Trade" everywhere on the dashboard (menu, boxes, i texts, reminders, review) and on the Chart tab level line. Name only.

## Added 2026-10-26 (Stop Loss tab, owner: "no trade will execute if no SL was set" / "the trade should be open but I should be notified.. and I will manually input SL if system missed it" / "Add all these options in the SL..")
- CHANGED same day (owner: "it's not right.. will take the trade and I get email reminder" / "Yes SL should be in menu / No if without SL still can save.. these can be done later. / Trades should also proceed when signal occurs.. nothing to stop ops from ongoing.. SL can be fixed manually."):
- Stop Loss is always in the menu (always picked). Save is allowed without it: "Measured on" left empty = saved as no SL, set it later.
- No SL set: the trade still opens (bot, shadow, Analysis) and Telegram + email "Trade-Bot - SL Alert": "No SL was set for this trade. Set it manually."
- The SL price cannot be worked out at an entry (not enough candles, qty unknown, or on the wrong side of the entry): the trade still opens and the alert says "SL could not be worked out for this trade. Set it manually." (`_hub_sl_missing_alert`). The bot's stop clean-up only touches its own stops, so a stop you place by hand stays.
- Measured on: % of ATR, % of price movement, Fixed $ amount (where the trade loses X USDT, from the real qty), Structure (swing low / high) (lowest low / highest high of N candles), Chandelier (highest high of N - value% x ATR; short mirror), Moving average (MA 1 / MA 2 value at entry). All worked out once at entry on normal candles (`hub.sl_level`), then fixed.
- Time stop (optional tick): at each candle close, open N candles (counted from the entry candle) and not in profit = close (`hub.time_stop_due`; live `_hub_time_stop`, reason "time_stop"; shadow "Time stop").
- Tests: tests/test_hub_stop_loss.py, tests/browser_stop_loss_check.py. Test plans (tests/_hub_plan.py) now carry a far SL (99.9% of price) so the old tests' Max Loss stays in charge.

## Added 2026-10-26 (Break-Even tab, owner: "I need another tab just after SL.. it's called Break-Even.. After TP - I will input 1, 2, 3 etc.. After ATR - I will input eg. 100 = 1xatr")
- New optional function after Stop Loss (menu now 14). Ticks: After TP (TP number 1, 2, 3; that TP must be used in Take profit), After ATR (ATR %, 100 = 1x ATR, + ATR length; ATR at the entry candle, fixed).
- When reached (whichever first), the Stop Loss moves to the entry price, only if that is tighter (`hub.be_setup` / `hub.be_due`). Live: checked in `_hub_ladder_check` on the live price; the nearest level then goes on the exchange. Shadow / Analysis: in `shadow_book.on_price`.
- Tests: tests/test_hub_break_even.py, tests/browser_break_even_check.py.

## Added 2026-10-26 (audit before live, owner: "Ok fix all")
- A. Stop-limit gone through and not filled: when the live price has gone THROUGH the stop-limit on the exchange (Stop Loss / Trailing Stop) and the trade is still open STOP_LIMIT_GRACE_S (2 s) later, the bot cancels the stop-limit's limit order and closes at market (reason "<level>_limit_not_filled", Telegram error message). Before, with Max Loss per Trade off nothing closed it. (`_hub_ladder_check`, timer `ladder.ex_passed_at`.)
- B. OKX: a restart with an open position no longer sends set-leverage (the margin-mode call re-sent leverage 1x).
- C. Leverage and margin mode are never touched while a position is open (startup). A hub pair sets the margin mode (Isolated / Cross) at every entry, before the leverage; refused = no entry this tick ("retry").
- D. A trade closed by the trailing level already broken / the time stop is not followed by an entry on the same candle in shadow + Analysis (as the live bot already did) and in the Pine script.
- Pine script: pine/STRATEGY_HUB_BLANK.pine (blank, sections 2-14 like the Hub). Not compiled here (no TradingView).
- Tests: tests/test_audit_fixes.py. Stress runs (scratch, not in the repo): random plans through the backtest and through the live bot against a simulated exchange (stops, stop-limits that gap, TP limits).
- STILL NOT PROVEN ON A REAL EXCHANGE: Binance stop-limit (algo STOP) and its trigger push, OKX stops / stop-limits / TP limits, flip speed, manual part close, Begins trailing.

## KIV - for the next session (owner: "all these other issues we discussed next session")
1. DONE 2026-10-24: one tab per function (13 tabs).
2. DONE 2026-10-24: "MA 1 vs MA 2" renamed "MA Crossover" (display name only; saved plans and the condition choices are unchanged; DONE 2026-10-24: the grey "(Moving Average Crossover Strategy)" line under the name is removed (owner: "Remove")). DONE 2026-10-24: Main for ENTRY / FLIP.
3. DONE 2026-10-24: Analysis tab replays each pair's Strategy Hub plan. (Was: replays the OLD Base V3 rules.) Owner decision: when the Hub starts, Analysis tracks every strategy created in the Hub (different strategy per pair); he will make a V3 version in the Hub and V3 is no longer used once it is live. Needs a rebuild of the tab. On hold: Hub functions first.
4. Combine ANY with Choppiness / Volume / ATR: they say OK for both sides and can cancel a signal.
5. DONE 2026-10-24: partial manual close checked; the ledger and the fixed-base total now record it.
6. DONE 2026-10-24: the shadow follows the whole plan on the live price and keeps running while real trading is paused.
7. DONE 2026-10-24: "Remove plan from pair" (Strategy Library build).
8. A take-profit fill while the bot is down may be missed in the ledger.
9. The Hub Pine script is not in this zip: after these changes the TradingView backtest will not match the bot unless it is updated.
10. Testing on Binance testnet / OKX demo (owner's action).

## Added 2026-10-27 (Take profit "Measured from", owner: "Do not remove anything but ADD this option.. for future use" then "please proceed to add")
- Owner: "I see the trade is more predictable rather than leave it to whatever entry price". Signal candle = candle 0; the entry is at market at once.
- Take profit tab: new choice "Measured from" (plan key tp.from): "Entry price" (the real fill, as before) or "Signal candle close" (snap.close, the normal close of the candle the entry is made on; ATR = that candle's ATR, as before). strategy_hub.tp_from / tp_base_price; hub_trading._hub_after_entry.
- A plan without the key = Entry price. upgrade_plan / complete_plan do NOT add the key (so old plans' rules hash does not change). The dashboard box starts blank like every other choice (no starter values); blank = key not sent = Entry price.
- "% of price movement" counts from the same base price. Break-Even After TP, the ladder and the ledger are unchanged (the TP memory still keeps the real fill as entry_price; the level prices use the chosen base).
- A TP price already behind the market at the fill (a long whose TP1 is below the fill) is still sent as a reduce-only limit; the exchange fills it at once (owner: the entry is at market, do not worry). If the exchange refuses it, the usual 3 tries then skipped + Telegram.
- Entry wait above 0: the base is the close of the candle the entry is made on (the owner said signal candle = 0). If he means the FIRST signal candle, the wait state must also keep that close: not built.
- Shadow / Analysis already counted from the signal candle close (paper entry), so they are unchanged. The Pine script already counts from the candle close (orders fill at the close), so it is unchanged.
- NOT proven on a real exchange (same as the other TP orders).
- Tests: tests/test_hub_tp_from.py (16), tests/browser_tp_from_check.py (11). Full suite 994 OK; all 17 browser checks pass.

## Added 2026-10-28 (Trailing Stop 4th type, owner: "I'm adding another TS" ... "The atr is the one we captured at signal" ... "Go")
- Owner: "Using signal candle the SL is 1 (editable) atr +/- signal candle high/low.. it moves closer to entry and into profit everytime a candle closes.. the SL only shrink nearer and into profit but never further from entry which may cause a loss. When live price hits the TS trade close."
- Trailing Stop tab, Type "Signal candle high / low +/- ATR" (strategy_hub.TRAIL_CANDLE; plan keys trail.type, trail.value = Distance in % of ATR (100 = 1 ATR), trail.atr_length). The 3 old types are unchanged. A plan saved before has none of it; complete_plan adds NO default for trail.value (rules hash of old plans unchanged).
- Rule: first level at the ENTRY from the signal candle (long: low - distance, short: high + distance); distance = value% x ATR of the signal candle (snap.ts_atr), kept in the ladder as tc_dist for the whole trade (owner chose the ATR at signal over each candle's own ATR). At every later candle close (hub_trading._hub_trail): level = the candle that just closed (low/high) -/+ the same distance; it takes charge only if tighter than the trailing level and the stop in charge (never back, same as Lowest low / highest high). A level already crossed by the price at the close closes the trade. Rests on the exchange as a stop-limit like the other trailing stops (nearest level); live price through it and still open after the 2 s grace = market close. Stop Loss and Max Loss stay behind it.
- Shadow / Analysis (shadow_book.on_candle, book["tc_dist"]) and the Pine script (trType option, trCVal input, tcDist) do the same. Pine NOT compiled in TradingView.
- ATR not available at the signal = no trailing for that trade (logged), the trade still opens.
- A restart rebuild of a lost ladder cannot recover tc_dist (not saved) = no candle trailing until the next trade; the ladder normally survives a restart (saved in hub state).
- The Distance box starts at 100 on a new form (owner: "1 (editable)"), like the Hold rule 25: tests/browser_hub_check.py lists it as an allowed start value. The ATR length is blank, the owner keys it in.
- Entry wait above 0: the signal candle is the candle the entry is made on (as for Take profit).
- NOT proven on a real exchange (same as the other trailing stops).
- Tests: tests/test_hub_trail_candle.py (18), tests/browser_trail_candle_check.py (14). Full suite 1012 OK; all 18 browser checks pass.

## Added 2026-10-28 (Presets, owner: "I need it in a tab as preset strategy BUT without the specs" ... "all discussed in but NO NUMBERS")
- Owner reason: his dev must not see his specs. So a preset holds ONLY rule choices (what is ticked / picked). NO number of any kind, not even EMA length, % of last profit, MACD Fast / Slow / Signal, timeframe.
- "Presets" button next to "Library" (Strategy Hub top). Presets "HMA Flip" and "MACD Cross" (V3 added 2026-11-02), listed with Details. Load needs the account and the pair chosen first, then a confirm. Load = a new form first (every box blank, Yes boxes Yes, the form's own starting boxes), then ONLY what the preset says. The pair's timeframe and margin mode are not touched. The function menu is as the preset says; every picked function still has to be opened (Execute stays off until then).
- The preset data is the constant HUB_PRESETS (frozen, read only) in app/static/dashboard.html between PRESETS-BEGIN and PRESETS-END. Load = hubApplyPreset. No server / Python change, so no plan_hash change.
- To change a preset: Load it, edit, "Save to library" under a name. Changing the preset itself = a new zip.
- HMA Flip holds: Candles; MA 1 HMA, MA rising / falling, ENTRY; MA 2 EMA with CTREND (not ENTRY / FLIP); flip boxes all Yes; Hold rule picked; Fresh Reversal ticked; No Mid-Trend Re-entry ticked; Fixed base amount; counter-trend size ticked (decided by MA 2); Max Loss per Trade, Stop Loss, Break-Even (After ATR), Trailing Stop (type Signal candle high / low +/- ATR; ATR length left for the owner), Take profit (% of ATR, from the signal candle close) picked; Shadow not picked.
- MACD Cross holds: MACD ENTRY + FLIP, "Histogram above / below 0" (not Zero line); Fixed base amount; Stop Loss; Trailing Stop type "Signal candle high / low +/- ATR"; Take profit not picked. Chart type, Hold rule, Max Loss, Break-Even, counter-trend, Shadow and No Mid-Trend Re-entry are left as a new form starts (owner decides).
- The Hub needs a condition ticked on MA 2 when MA 2 carries the CTREND tick (validate_plan: "MA 2: tick at least one condition"). The HMA Flip preset leaves it for the owner, he has not said which.
- Tests: tests/test_hub_presets.py (9, runs the preset block with Node; skipped if Node is missing), tests/browser_presets_check.py (41). The tests use arbitrary numbers only.

## Added 2026-11-01 (Cooldown after a loss + Loss limit, owner: "Just build 1&2" after "Open trade must stay open.. no trade should be distracted by external factors.. only it's own price action")
- Two new Hub tabs at the END of the menu (16 functions now): "Cooldown after a loss" and "Loss limit". Both are optional (picked = on), no starting numbers, not in the presets. A plan without the keys behaves exactly as before (complete_plan adds nothing, so the plan hash of an existing plan is unchanged).
- BOTH ONLY STOP A NEW ENTRY. An open trade is never touched (its stops, TP, trailing stop, flip rules stay in charge).
- COOLDOWN (plan key cooldown: on, candles, optional after_losses): a trade that closes completely with a result below 0 (every fill added up, net of fees; new HubState fields res_open / res_pnl / loss_streak / cooldown_through) skips the next N candle closes. Blocked signal candles: open time up to last_processed_candle + N x candle length (hub.cooldown_after_close). A flip's opposite entry at the same close is blocked too. A trade that ends at 0 or in profit ends the losses in a row and starts nothing. "Losses in a row" empty = after every loss; N = only from the Nth loss in a row, and again after every further loss until a win. Gate: HubMixin._hub_entry_gates (called in _hub_try_open after the shadow gate); the decision of that candle is dropped, the next candle is judged fresh. Result tracking works for both sizing methods (independent of the fixed-base total).
- LOSS LIMIT, 3 levels, each daily and / or weekly, USDT, empty = none: (1) this strategy = plan key loss_limit (on, daily, weekly) counts that pair (account + symbol); (2) one account and (3) all accounts together = the "Loss limits" button next to Presets / Library (app/loss_limits.py, file DATA_DIR/loss_limits.json on the Railway volume, routes GET / POST /api/loss-limits). Counts the realized net result in the LEDGER (every fill, every pair, also Base V3 pairs) since the day / week started; a win counts against a loss; an open (unrealized) loss is not counted. Reached (lost >= amount) = no new entry until the period is over, one Telegram + one email per limit per period (the "sent" marks are in the same file), the decision text says LOSS LIMIT. Day = 00:00 SGT, week = Monday 00:00 SGT (hard-coded, owner is in SGT).
- Only Strategy Hub pairs are gated. A Base V3 pair is NOT stopped by the account / entire limit (its results are counted). Shadow mode, the Analysis tab and the Pine script do NOT use Cooldown or Loss limit.
- An unreadable loss_limits.json is never overwritten; the strategy level still works and the error is logged. A broken check lets the entry go ahead and says so in the log.
- The older account setting max_account_exposure_pct (risk_guard, exposure cap) already existed: not changed.
- Not proven on a real exchange: the whole path (testnet / demo first). The numbers come from the ledger, so a fill whose price could not be fetched (confirmed = False, gross) counts as it was recorded.
- Tests: tests/test_hub_loss_cooldown.py (49), tests/browser_loss_cooldown_check.py (35); browser_hub_check and browser_info_icons_check now expect 16 functions.

## Added 2026-11-01 (RSI crosses its own MA, owner: "Add in rsi crossing it's own moving average (sma, ema, wma, hma)", then "Yes include alma .. let's go", "add in pine script")
- RSI got a 4th Condition tick: "RSI crosses its own MA" (strategy_hub.RSI_MACROSS). Long = RSI crosses UP through its own moving average, short = DOWN through it (cross rule = same as Pine ta.crossover / ta.crossunder: now above and before at or below). The average is taken of the RSI line (not of the price). On for that candle and N more (the shared RSI "N candles (signal stays on)" box).
- New RSI plan keys (only when this condition is ticked): ma_type (SMA / EMA / WMA / HMA / ALMA), ma_length (whole number, at least 1), ma_alma_offset + ma_alma_sigma (only when ma_type = ALMA; sigma at least 0.0001, same rule as MA 1 / MA 2), n. The RSI "length" is the RSI itself. Absent keys = condition not used (an older plan loads and behaves exactly as before). complete_plan fills inert values like the other blocks.
- Maths reuses the Hub's MA helpers (_ma: pine_sma / pine_ema / pine_wma / pine_hma / pine_alma). Frame column "rsi_ma" exists only when the condition is ticked.
- Can be ticked with the other RSI conditions (ALL ticked must agree). Works as ENTRY, FLIP and as "Counter trend decided by" RSI like the other conditions.
- Dashboard: Indicators -> RSI: new tick + "RSI average: Type / Length / ALMA offset / ALMA sigma" boxes (empty, owner keys them in; ALMA boxes only show when the type is ALMA); info icons rsi_ma_type, rsi_ma_length, rsi_ma_alma_offset, rsi_ma_alma_sigma. Not in the presets.
- Pine (STRATEGY_HUB_BLANK.pine, group "3. Indicators - RSI"): rsMaOn, rsMaType, rsMaLen, rsMaOff, rsMaSig; rsiMa = maOf(...) of the RSI; rsMaU / rsMaD with the same N. NOT compiled in TradingView here (no way to do it in the workspace) - run it once and check the inputs show.
- Tests: tests/test_hub_rsi_ma_cross.py (36; the expected values are worked out by SEPARATE code in the test: pandas / loops for RSI, SMA, EMA, WMA, HMA, ALMA), tests/browser_rsi_ma_cross_check.py (28). tests/test_strategy_hub.py (the "each setting is required" test) knows the new keys. Full suite 1106 OK; all 21 browser checks OK; 18 deliberate-break checks all caught.
- Not proven on a real exchange / against TradingView numbers on real candles.

## Added 2026-11-02 (Loss limit in %, owner: "Have both so user can choose $ or %", base = "A" then "Go")
- Every Loss limit can be a USDT amount (as before, unchanged) OR a % of the account balance at the start of the day / week. Plan keys (strategy level, under loss_limit): daily_pct, weekly_pct (0.01 to 100). Account + entire levels (DATA_DIR/loss_limits.json): daily_pct / weekly_pct next to daily / weekly. At least one of the four is needed when "on". A plan without the new keys behaves exactly as before. If a USDT amount and a % are both keyed in for a period, the first one reached stops the entries.
- BASE A: the % is taken of the balance SAVED at the start of the period, worked out once, so it is one fixed USDT number for the whole day / week (it does not shrink while you lose). Stored in loss_limits.json under "base": {"d:<start epoch>": {account_id: balance}, "w:<...>": {...}} (keeps the last 3 days / 3 weeks). Balance = exchange get_equity() (total margin balance, includes open-trade result).
- How the balance is saved: (1) HubMixin._hub_note_balance at the start of every Hub tick (only when a % limit applies: loss_limits.balance_due; one read per account per day / week; a failure is logged at most every 10 min and never stops the tick); (2) loss_limits.check(..., equity=) saves it too if still missing (the gate reads get_equity only when a % limit applies). Saved value = balance now minus the net result the LEDGER shows as closed in that account since the period started (so a late first sight still gives the start balance). Deposits / withdrawals are not known.
- Strategy and account levels use that account's saved balance; "all accounts together" uses the saved balances added up (accounts with no Strategy Hub pair running save none, so they are not in the sum).
- A % limit whose base is unknown cannot be checked: it is skipped (logged) and the entry goes ahead; a USDT limit in the same plan is still checked.
- Email / Telegram: once per limit per period; the % limit has its own mark ("...:daily%:...").
- Dashboard: Loss limit tab has Daily % / Weekly % boxes (empty = none, no starting number); the Loss limits box has a USDT and a % box per limit and shows "= X USDT (of Y USDT at the start of today)" once the balance is saved. Help: loss_daily_pct, loss_weekly_pct, fn.losslimit, lossbox.open.
- Open trades are never touched; Shadow mode, Analysis and Pine do not use the Loss limit.
- Tests: tests/test_hub_loss_limit_pct.py (50), tests/browser_loss_cooldown_check.py (49); tests/test_hub_loss_cooldown.py data shape updated (4 keys). 20 deliberate-break checks all caught.
- Not proven on a real exchange (testnet / demo first).

## Added 2026-11-02 (V3 preset, owner: "I'll have the V3 in as a preset but I will have the option to choose and amend every function and/or indicator" ... "Follow the main No mid.... function")
- Third preset 'V3' in HUB_PRESETS (dashboard.html, PRESETS-BEGIN..END). Rule choices only, NO NUMBERS, same as the other two. Load / frozen / no server change as before.
- Choices: Heikin Ashi; Candle colour + MA 1 (HMA, rising / falling) + MA 2 (EMA, price above / below), ALL; flip boxes all Yes; Hold rule; Fresh Reversal ticked; allow_reentry false (No Mid-Trend Re-entry ticked); Allocation % of balance + Counter-trend by EMA; Max Loss % of available balance; Shadow; no SL basis (SL is always in the menu, empty = no SL); no BE / TS / TP.
- Not identical to the Base V3 code: V3 re-enters next candle after a flip, the Hub at once. V3 allowed mid-trend re-entry; the preset has it off (owner choice).
- Tests: tests/test_hub_presets.py (14), tests/browser_presets_check.py (59).

## Added 2026-11-02 (RSI MA Cross preset, owner: "No more rsi reading since I'm using cross over of RSI itself.. the cross can happen anywhere" / "entry size use Fix amount + % of last profit" / "the rest are ok.. but let me have option to choose and edit")
- Fourth preset 'RSI MA Cross' in HUB_PRESETS (dashboard.html, PRESETS-BEGIN..END). Rule choices only, NO NUMBERS. No server change.
- Choices: Candles; RSI (only 'RSI crosses its own MA') + MA 1 (HMA, rising / falling) + ADX / DI, ALL; flip boxes all Yes; Hold rule; Fresh Reversal and No Mid-Trend Re-entry ticked; Fixed base amount + Counter-trend by MA 1 (lock_pct = '% of last profit' left empty); Max Loss, Stop Loss, Trailing Stop (type empty), Shadow, Cooldown picked; no BE / TP / Loss limit.
- Not proven on real candles; the trailing type, Max Loss basis and Stop Loss basis are the owner's pick.
- Tests: tests/test_hub_presets.py (19), tests/browser_presets_check.py (75).

## Added 2026-11-03 (EMA Cross preset, owner: "Cross ema ... Trailing Stop with 0 x atr ... Entry is base + 50% of last gains, no adx" / "Add rsi and adx just in case" / "I may want to use next time just have it but off" / "Ok let's have it preset")
- Fifth preset 'EMA Cross' in HUB_PRESETS (dashboard.html, PRESETS-BEGIN..END). Rule choices only, NO NUMBERS. No server change.
- Choices: Candles; MA Crossover only (MA 1 EMA crossed MA 2 EMA within N candles); ADX / RSI present but NOT ticked; flip boxes all Yes; Hold rule not picked; Fixed base amount (lock_pct = '% of last profit' left empty); Trailing Stop type 'Signal candle high / low +/- ATR'; no BE / TP. Max Loss, Stop Loss, Shadow, Cooldown, Loss limit, counter-trend, Combination left for the owner.
- validate_plan asks for one condition ticked on MA 1 and MA 2 when MA Crossover is ticked; the preset leaves those to the owner. Test proves they do not change the entry signal.
- Trailing Stop distance: 0 is refused (min 0.0001); the form starts at 100.
- Owner's EMA lengths for this idea: 9 / 21 on 1h (his words, not in the preset). Not tested on real candles.
- HEDGE MODE (KIV): owner wants a pair to hold a long and a short at once (RSI + MFI low = buy batches, each batch closes whole at its own take profit % after fees). Binance docs confirm Hedge Mode by API (account-wide, rejected with open orders/positions, positionSide required, reduceOnly not allowed). OKX long_short_mode with posSide: OKX's own restrictions not confirmed. Needs a separate Hedge Mode account. NOTHING BUILT. Also needs an MFI indicator.
- Tests: tests/test_hub_presets.py (27), tests/browser_presets_check.py (95).

## Added 2026-11-03 (MA 1 over MA 2 preset, owner: "moving average 1 over moving average 2 or moving average 1 under moving average 2" / "let's have moving average 1 over moving average 2 as the next preset")
- Sixth preset 'MA 1 over MA 2' in HUB_PRESETS (dashboard.html, PRESETS-BEGIN..END). Rule choices only, NO NUMBERS. No server change. Plan holds only ind.mx = {entry: true, modes: ['MA 1 above / below MA 2']}.
- Nothing else is set by the preset (owner did not name anything else). MA types / lengths and the one condition each on MA 1 and MA 2 are his to pick.
- Tests: tests/test_hub_presets.py (33), tests/browser_presets_check.py (110). The signal test compares entry_long / entry_short with pandas ewm / rolling.

## Added 2026-11-04 (Exit when + Bollinger Bands + Supertrend, owner: "Go" / "Everything"; "always gimme option.. it has to be flexible")
- Exit when (strategy_hub.exit_cfg / exit_decision / exit_after; hub_trading._hub_candle; shadow_book.on_candle; Pine G15). Optional, absent = OFF. Works with any plan. Order on a candle: flip_decision first, then exit_decision. Wait = close with reason "exit_when", stay flat; instance._reset_position_state sets hub.reentry_forced so the same side is blocked until its entry signal ends even when allow_reentry is on. Flip = closes and opens the opposite side, uses the flip wait. The Hold rule does NOT stop Exit when.
- Candle prev_opposite (optional key). Bollinger `bb` (CT_BLOCKS member; CTREND = Bollinger half, middle line = counter). Supertrend `sup` (not a CT block). Old plans get bb/sup OFF in upgrade_plan.
- Trailing type TRAIL_SUP 'Supertrend line': sup_trail_level() gives the line only if on the protective side; set at entry and at each candle close; only tightens.
- hub_state.HubState.reentry_forced (default False; old state files load).
- Tests: tests/test_hub_exit_bb_sup.py, tests/test_pine_text.py, tests/browser_exit_bb_sup_check.py. Older browser checks were updated for the deliberate counts (17 functions, 13 indicators, 5 trailing types).
- Pine script not compiled (TradingView needed). Real exchange untested.
- Parked, not built: RSI with Bollinger (which condition?), ADX / flip changes, Hedge Mode (KIV).

## Added 2026-11-05 (Change an open trade, owner: "Yes do that.. go"; "MUST have both options")
- strategy_hub.AMEND_PATHS / AMEND_GROUPS / amend_diff / amend_merge: the allowed settings (trail, sl, be, tp, exit_when, hold.adx_level, sizing.stop_on/stop_basis/stop_pct/stop_usd). amend_merge = a copy of the running plan (inst._plan) with only those taken from the new plan; the rest waits (the full new plan is saved as always and applied at flat via _pending_restart_on_flat).
- hub_trading.hub_amend_open_trade(new_plan, stops) (stops = "tighter" | "looser"): works the new levels out from the SIGNAL candle (_hub_entry_snapshot, a read-only copy of _hub_fetch using the NEW plan) as if the setting had been on at entry; adopt() keeps the tighter-only rule; _hub_amend_tp cancels the resting TP order (only after the new levels are worked out) and rebuilds the memory (filled levels stay); then replaces inst._plan with the merged plan and calls _hub_sync_exchange_stop. Guards: _hub_amending flag (TP placing and the memory-level watch wait, one change at a time); a trade that closes meanwhile = RuntimeError, nothing committed.
- hub_api.apply_plan(..., amend=None): amend = {"when": "now"|"next", "stops": "tighter"|"looser"} (check_amend validates; stops required for "now"). Response gets "amend_result" ({status: done|next|not_now|error|no_open_trade, applied, kept, waits}). A failed change never undoes the saved plan. PlanBody.amend is optional (old callers unchanged).
- Dashboard: #hub-amend-box in the review dialog, shown only when GET /status/{acc}/{sym} says IN_POSITION; no radio pre-selected; Confirm disabled until chosen; HELP keys amend.now / next / tighter / looser.
- After a "tighter" run that kept looser values, inst._plan equals the new plan, so saving the same plan again shows no difference. Use "Allow looser too" with a changed value if you want the looser level later.
- Tests: tests/test_hub_amend_open_trade.py, tests/browser_amend_check.py. Real exchange (Binance and OKX) untested.

## Added 2026-11-05 (Editable menu: names, grey texts, order; owner: "go", one setting for all strategies, saved on the server)
- app/hub_menu_layout.py: load()/save() of DATA_DIR/hub_menu_layout.json = {order:[ids], names:{id:text}, descs:{id:text}} (validated: id pattern, <=60 / <=300 chars, no duplicates; broken file = original menu). Routes GET/PUT /api/hub-menu (api/__init__.py; PUT needs the mutating deps). Schema MenuLayoutBody.
- dashboard.html: HUB_FUNCS stays the ORIGINAL list; hubFns() returns it re-ordered with the owner's names/texts (unknown ids ignored, missing ids at the end). Menu, summary, pane headers (#hubtitle_<id>, #hubdesc_<id>) and 'Function N of M' use hubFns(). Edit window #dlg-hub-menu. Only changed texts are stored.
- Tests: tests/test_hub_menu_layout.py, tests/browser_menu_check.py.

## Added 2026-11-05 (Support / Resistance, owner: "go"; "SR should be determined by the charts not me"; "Sr can also be a main strategy right?"; "Ok let's include Sr in e menu.")
- app/sr_engine.py (pure numpy, no look-ahead: levels are those known at bar i-1; a swing is known N candles after it forms). compute(o,h,l,c,v,atr,cfg) returns per-bar arrays: long, short, origin (0 none /1 bounce /2 breakout /3 retest), stop_long/short, exit_bl/bs (bounce exits), exit_kl/ks (breakout/retest exits), s1,s2,r1,r2,mid, lock, lock_dir, break_up/dn, conflict, plus levels_now (table rows).
- Plan: plan["sr"] (all ticks/numbers inert until the owner ticks and keys them in; nothing preselected). Ticks in strategy_hub.SR_TICKS, numbers SR_NUMBERS, helpers sr_cfg / sr_exit_ticked / sr_entry_ticked / sr_stop_on. Menu function "sr" (18 functions) and Indicators row "sr" (14 rows). _validate_sr; a stand-alone SR plan (no other indicator) is allowed.
- compute_hub adds columns sr_long/short, sr_origin, sr_stop_*, sr_xb_*, sr_xk_*, sr_s1/s2/r1/r2/mid/lock, sr_lock_dir, sr_break_*, sr_conflict; out.attrs["sr_levels"]. sl_level = the nearer of the SR stop and the normal Stop Loss. sr_exit_decision(direction, snap, plan, origin). Live: hub_trading (_hub_do_enter stores hub.sr_origin, SR exit block after Exit when, reason "sr_exit"), instance.py (resets sr_origin; sr_exit forces the wait like exit_when), shadow_book.py (same exit, "Support / Resistance exit"), backtest through shadow_book.
- Chart tab: chart_api.sr_payload (lines, B/K/R markers, table rows) and dashboard.html (lines, badges, table with tickable columns kept in localStorage chartSrCols).
- Honest limits: a bounce and a breakout on opposite sides cannot happen on one candle at one level (the conflict guard is an edge safety; retest long + breakout short can conflict). A failed up-break becomes a fresh down-break lock. "Trailing stop takes over" NOT built. Exits are judged at candle close on the nearest levels at that moment. The wait blocks only SR entries, never an open trade or other indicators. The SR stop applies only to trades SR entered. SR changes while a trade is open wait for the next trade. The Pine script does NOT mirror SR. Real exchange untested.
- Tests: tests/test_sr_engine.py (54), tests/test_sr_hub.py (35), tests/browser_sr_check.py (47); 31 deliberate-break checks all caught (scratchpad mutate_sr.py).

## Added 2026-11-05 (two options, owner: "Good to have both with options for me to choose" / "Both")
- Trailing Stop "Signal candle high / low +/- ATR": new optional trail.candle_start = TCS_TIGHTER ("Tighter than the Stop Loss", the old behaviour; also what a plan WITHOUT the key means) or TCS_PROFIT ("Only once in profit") + trail.candle_profit (% of the signal candle ATR, inert 100 in complete_plan). Profit mode: no trail level at the entry; ladder["tc_arm"] / book["tc_arm"] = entry +/- candle_profit% x signal ATR (helpers candle_start_profit / candle_arm_price / candle_armed); at a candle close, while ts is still None, the trail may take charge only if that candle CLOSED at or beyond tc_arm; once it has taken charge it only tightens as before. Its level is still candle low/high -/+ the distance, so it can sit slightly on the wrong side of the entry. hub_trading (_hub_new_ladder, candle-close block, amend), shadow_book and Pine (trCStart, trCProfit, tcArm) follow it.
- Stop Loss "Structure": new optional sl.struct_mode = SLS_EXTREME (old, lowest low / highest high of sl.length candles; also the meaning of no key) or SLS_SWING ("Real swing point (N candles each side)") + sl.struct_n (inert 5). compute_hub adds sl_piv_low / sl_piv_high only for the swing choice (_sl_pivots, using sr_engine.swing_points; a swing is known N candles after it formed, nothing looks ahead; latest swing low BELOW the close / swing high ABOVE it, looks back at most 500 swings). HubSnapshot.sl_piv_low/high; sl_level uses them; a missing or wrong-side level = the usual "SL could not be worked out" path (trade still opens). sl.length is not needed with the swing choice. Pine: slStruct, slPivN, pvLo/pvHi arrays (ta.pivotlow / ta.pivothigh; NOT compiled here).
- Dashboard: pick-one boxes trail_c_start / sl_struct_mode (the old behaviour is ticked on load), numbers trail_c_profit / sl_struct_n (empty, no preset); hubBuildPlan drops the keys when the old choice is ticked so re-saving an older plan is not seen as a change to an open trade; hubVisible got an optional f.cond().
- Tests: tests/test_hub_trail_swing_options.py (42), tests/browser_trail_swing_check.py (25), tests/test_pine_text.py (+1); 26 deliberate-break checks all caught (scratchpad mutate_opts.py). Real exchange untested.

## Added 2026-11-05 (EMA Band + Parabolic SAR, owner: "So let's build this ema band." / "Include sar too")
- Plan keys: plan["eb"] = {length, n, long_low, long_high, short_high, short_low} (ticks optional, inert False; numbers keyed by the owner, nothing preset) and plan["sar"] = {step, limit} (empty until keyed). Blocks "eb" and "sar" in BLOCKS (Indicators list, before "sr"), titles "EMA Band" / "Parabolic SAR"; both are in CT_BLOCKS (counter-trend sizing deciders). Support/Resistance stays OUT of that list on purpose. upgrade_plan adds the keys to older plans; _INERT_BLOCK holds the inert values.
- EMA Band: 3 EMAs of the same length on high (top), close (middle, not used by any rule) and low (bottom) (pine_ema). Long = close crosses ABOVE the bottom line (long_low) and/or the top line (long_high); short = close crosses BELOW the top line (short_high) and/or the bottom line (short_low); several ticks = OR. "Where price came from": the close was on the other side of that line within the last N candles (1 = the candle just before); the signal stays on for N candles after the cross while the close stays on the new side. Validation: at least one line tick is needed.
- Parabolic SAR: pine_sar(h,l,c,start,inc,maximum) = Python port of Pine's ta.sar (first value on the 2nd candle); step is used as start AND increase, limit as maximum. Long while the dots are below the close, short while above. Validation: limit >= step.
- compute_hub columns: eb_top, eb_mid, eb_bot, eb_long, eb_short, sar_dots, sar_long, sar_short.
- Dashboard: HUB_BLOCKS 'eb' / 'sar', HELP keys ind.eb, eb_*, ind.sar, sar_*, HUB_CT_BLOCKS/HUB_CT_OPTS (+eb, sar). Pine: eb/sar inputs, ebL/ebS, saCalc, blockL/blockS, cnt() now 15 args, ctBlock chain (NOT compiled here).
- Honest limits: the EB middle line is NOT drawn on the Chart tab (the Chart tab draws no bb/supertrend lines either). SAR is not checked against TradingView. Pine not compiled. Real exchange untested.
- Tests: tests/test_eb_sar.py (32), tests/browser_eb_sar_check.py (29); 31 deliberate-break checks all caught (scratchpad mutate_ebsar.py).
- 2026-11-05 audit fix (owner: "Fix all"): Pine EMA Band "was on the other side within N candles" (within() = ta.barssince) was on the right of an `and`; Pine v6 skips that side when the left is false, so it would have been wrong on TradingView. Now ebWasBelowBot / ebWasBelowTop / ebWasAboveTop / ebWasAboveBot are worked out on their own lines every candle. Python bot unaffected. tests/test_pine_text.py has a rule that no ta.* / within() may sit on the right of and / or. Pine still NOT compiled here.

## Added 2026-11-06 (whole-project audit; owner: "Fix and give me complete zip file now")
- Fixed: app/manager.py and app/okx_manager.py start_all_enabled - the error line for a shadow-only pair that fails to start used an undefined name (label), which raised a NameError out of the loop (later pairs not resumed, possibly the app not starting). Now logs normally and carries on.
- Fixed: app/api/__init__.py forgot_password used AuthStore (not imported) -> server error, no reset email, and the error showed which username exists. Now uses auth_store.RESET_TOKEN_TTL_SECONDS.
- Tests added: tests/test_manager.py, tests/test_okx_manager.py (shadow-only start failure), tests/test_app_imports.py (forgot password sends the email; wrong username = same reply, no email). Each fails on the old code.
- Audit left alone (low risk, not asked): unused imports/variables, dead helper functions, corrupt reconciliation file skipped silently, list_all_pending_reconciliations never called, walk-forward / Monte Carlo POST without CSRF, failed-login dict unbounded, sessions survive a password change. Unit tests run ~80% of app statements; main.py and the web layer are covered only by import checks (FastAPI is not installed in the dev sandbox).
