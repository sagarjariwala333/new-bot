# Build notes (owner only) - Strategy Hub Bot

- The Strategy Hub is the only way a pair gets a strategy. No preset strategy, no starter values.
- The setup page and Add pair form hold pair-level settings only. All strategy settings are Hub boxes. The old Hull code is still in the files, unreachable (not deleted).
- Order-id prefixes `hullbot_` / `hullbotokx` are unchanged on purpose, so orders already on an exchange are still recognised.
- NOT proven on a real exchange: TP limit orders, trailing-stop moves, shadow mode. Test on Binance testnet / OKX demo first.
- The older Hull builds and their documents are in the earlier zips, not in this one.

## 2026-11-15 Arbitrage tab
Built inside the hub (`app/arb/`, 4 steps: scanner, mock, live, locks). See `docs/ARBITRAGE_FOR_DEV.md`. Untested on a real exchange: run `docs/SMOKE_TEST_ARBITRAGE.txt`.

## V-16.11.2026.1 - Enter with what is available (balance short)
- Plan keys: `sizing.short_enter` (bool, default False) and `sizing.short_buffer` (0-50, None = 2).
- `strategy_hub.short_balance_fit(sz, margin_needed, available)` returns (margin_to_use, shortfall). OFF or enough balance = no change.
- `hub_trading._hub_fit_short_balance` runs right after `round_qty` and before the `qty <= 0` check. It shrinks qty to the usable balance, logs, sends Telegram + email once per candle, and never raises. For the fixed-base method it also sets `entry_amount` to the real margin so `trade_amount` / the next total and the max-loss stop use the real margin.
- Dashboard: two fields in the Margin alert group, info texts, review line. Plan review line "Balance short".
- Tests: `tests/test_hub_short_balance.py` (17). Full suite: 1688 OK.

## V-16.11.2026.2 - Shadow: start after every win
- Plan key `shadow.every_win` (bool, default False). Validated only when shadow.on.
- `base_v3_tracker.apply_real_close(..., every_win=False)`: starts the period when `use_shadow and pnl > 0 and (new_high or every_win)`. The tracker peak still moves only on a new high.
- `instance._record_close` passes `every_win` from the plan; `hub_backtest` uses the same rule (`every_win or equity > peak`).
- Dashboard: tick under Shadow mode, info text `shadow_every_win`, review line. Tests: `tests/test_hub_shadow_every_win.py` (6). Full suite: 1694 OK.

## V-16.11.2026.3 - Shadow: enter right away when the pause ends
- Plan key `shadow.resume_now` (bool, default False). `hub_trading._hub_shadow_closed`: with it ON, `resume_block_time` is not set, so the real entry is allowed on the flip candle where the last paper trade ends (the shadow step runs before the real orders in a tick). OFF = old behaviour (the real entry comes one candle later).
- Hub backtest already counts that same-candle entry (it behaves like ON).
- Tests: live scenario in tests/test_hub_shadow_every_win.py (win, paper short, flip to long: OFF enters on the next candle, ON on the flip candle). Full suite below.

## V-16.11.2026.4 - Preset "Ultimate Super Trend"
- dashboard.html HUB_PRESETS: new entry `ultimate_supertrend` (rule choices AND the owner's numbers; the pair's timeframe is never touched by a preset). `hubApplyPreset` now passes the preset's `main_entry` / `main_flip` to `hubMainOptions`.
- Stop Loss is a core function: the preset leaves "Measured on" empty (= no SL saved as off).
- tests/browser_presets_check.py: 7 presets listed; the "no number anywhere" check skips this preset; new block of Ultimate Super Trend checks (119/119 pass).


## V-17.11.2026.1 - Exit when: Divergence (RSI / MFI / CCI)
New third signal. Per oscillator on/off (all OFF), own numbers. Pivot logic = the RSI Divergence entry condition (osc_divergence). Found-time guard: a divergence found at or before the trade's entry candle is ignored (entry_candle_time live, entry_time in shadow_book/backtest). compute_hub columns xd_<osc>_osc/_ok_l/_t_l/_ok_s/_t_s; HubSnapshot.xdiv; exit_decision(direction, snap, plan, entry_time). MA exit flags are not computed for this signal. Old plans unchanged. Not built: count the Nth divergence; no Pine mirror.


## V-17.11.2026.2 - Divergence exit: 7 oscillators + any timeframe
DIV_INDS now rsi, mfi, cci, macd (histogram), stoch (%K smoothed), obv, cmo; DIV_SPEC holds each one's number boxes and whether it has a band. Each block has tf (Chart or any exchange interval in DIV_TF_MS). strategy_hub.htf_to_chart maps a signal from another timeframe's candles onto chart candles: at the close of chart candle i, candles of that size closed by then are known; a signal counts once on the first chart candle that can see it (latest if several). compute_hub(df, plan, htf) raises HtfMissing when needed candles are absent. app/hub_htf.py fetches them (live: cached_klines, cap 1500; Analysis: cap 5000 and attached as df.attrs['htf']). hub_trading passes them at both compute_hub sites. Note: the Pine test (1700 file) reads a higher candle one chart candle later (it uses the previous closed higher candle), so Pine and Hub can differ by one chart candle.


## V-17.11.2026.3 - every candle size
DIV_TF_MS now also 2d, 5d and 1M (union of Binance and OKX sizes). 1M closes on the first of the next calendar month (div_tf_close); OKX map has 2d, 3d, 5d, 1w, 1M. An unsupported size on the chosen exchange shows the exchange's own error.


## V-18.11.2026.1 - Timeframe box on every indicator + MFI / CCI / Stochastic / CMO / OBV as Entry / Flip indicators
New blocks mfi, cci, stoch, cmo, obv in BLOCKS (after rsi): MFI / CCI / Stochastic / CMO get RSI's 4 conditions (level, overbought-oversold back inside, divergence, crosses its own MA); OBV gets 3 (above / below its own MA, rising / falling, divergence). OSC_MODES / osc_mode_keys / SCHEMA. All OFF in old plans (upgrade_plan), also usable as Main and as counter-trend decider (CT_BLOCKS).
Every block except mx and sr has an optional tf (Chart default, any DIV_TFS size; OPTIONAL_KEYS, so old plans are unchanged). compute_hub now runs blocks through _run_blocks: Chart blocks on the chart candles; every other size on candles of that size (htf[tf]), then each value / signal is forward-mapped to chart candles by the closed-candle rule (last candle of that size closed by the chart candle's close; nothing before the first one). MA Crossover follows MA 1 / MA 2 (read from the mapped lines). div_tfs_needed also lists the sizes of the ticked indicators, so hub_htf.fetch_htf / Analysis fetch them; a missing size raises HtfMissing. An indicator on another size that nothing uses asks for nothing. chart_api S/R forces tf Chart (it needs only normal candles). Trailing Stop Supertrend line and MA exit lines follow the block's own tf.
Dashboard: HUB_OSC_DEFS, HUB_TFS, tf field first in every block, HELP for all, review text adds [on <size> candles]. Pine strategies are NOT changed (only RSI / MFI / CCI divergence there).
Tests: tests/test_hub_indicator_tf.py (24), tests/browser_indicator_tf_check.py (30); test helper plan and counts updated. test_instance_safety has 2 order / balance dependent tests that also fail without this change.


## V-19.11.2026.1 - Add Pair limit 5, UST preset Shadow OFF
dashboard.html renderPairs: Add Pair (Binance) disables at 5 pairs, not 3 (the server and OKX already allowed 5). Preset Ultimate Super Trend: shadow = {on: false} (owner 2026-11-19); description text updated; its tests updated.


## V-19.11.2026.2 - Higher leverage on a proven divergence
plan["sizing"]["boost"] = {on, leverage, div:{combine, keep, turn, price, <osc>:{on, tf, numbers...}}} (same shape and validator as Exit when -> Divergence: _validate_div_cfg, _div_signals; error text starts "Higher leverage"). Optional, absent = OFF. compute_hub adds boost_long / boost_short: LONG = a BULLISH divergence found within the last keep chart candles (and, if ticked, oscillator back inside its band / close beyond the swing between the two pivots); SHORT = BEARISH; ticked oscillators ANY / ALL; each oscillator has its own timeframe (div_tfs_needed lists it). entry_size: when on and the flag of the entry side is set, leverage = boost leverage (not for counter-trend sized entries); amount, allocation and Max Loss untouched (the stop % comes from the entry amount, so the stop sits closer). HubSnapshot.boost_long / boost_short; hub_trading logs the boost; paper tracker (shadow) and backtest use entry_size so they follow; backtest trade rows now carry "leverage". Dashboard: Entry size function, card "Higher leverage on a divergence" (hubBoostFields / hubDivFields), HELP, review line. Pine strategies are not changed (no boost there).
Tests: tests/test_hub_leverage_boost.py (15, incl. the real entry path), tests/browser_leverage_boost_check.py (15).

## V-21.11.2026.1 - Higher leverage on a divergence is its own menu function
dashboard.html: new HUB_FUNCS item "boost" (switch boost_on, right after Entry size) with its own card; the card is no longer inside Entry size. Same plan shape (plan["sizing"]["boost"]) and same server code as V-19.11.2026.2 - only where it sits in the menu changed. All 7 divergence indicators (RSI, MFI, CCI, MACD histogram, Stochastic, OBV, CMO), each with its own timeframe (can differ from the pair's). The leverage box starts EMPTY (no default number; server default is None, inert while OFF). Help text fn.boost added.
