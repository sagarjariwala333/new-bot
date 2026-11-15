# Arbitrage tab - notes for the dev (V-15.11.2026.5)

Read this before touching the "Arbitrage" tab. The tab was built natively inside the hub (code in `app/arb/`).
**Everything starts OFF.** Nothing is read, mocked or traded until the owner switches it on and saves.
**Never tested against a real exchange.** Do `docs/SMOKE_TEST_ARBITRAGE.txt` on testnet / demo first, then once live with a tiny size.

## What it is
Three modules, each with its own on/off and mock/live switch:
- **Spatial**: same coin, Binance vs OKX (futures and/or spot).
- **Funding**: spot long + perp short on the same exchange.
- **Triangular**: needs the confirm tick AND the typed word YES in the tab (locks again after every restart). Stays off otherwise.

A scanner reads public prices (read-only) and shows the best gaps. Mock modules trade pretend money on those prices.
Live modules send real orders. A "live-mock" shadow runs a pretend copy next to each live module.

## Gates before any real order (all must pass; the reason is shown in the tab)
1. Module mode = live, and the owner typed LIVE when saving.
2. Risk boxes filled (blank = refused): max size per trade, daily loss, margin cap (futures). 0 = off.
3. API keys entered (Binance futures, Binance spot [optional, falls back to futures key], OKX) and working.
4. Futures accounts in ONE-WAY position mode (hedge mode is refused).
5. Prices fresh. Kill switch (STOP.txt or the "Stop new trades" button) off. Daily loss limit not hit.
6. Per trade: coin has no existing position on the exchange, enough balance, margin cap not exceeded.

## Hub coexistence
- Isolated margin only.
- If the arbitrage key is the SAME key as a hub account, every coin that hub account trades is skipped (`hubmatch.py`).
  With a separate sub-account key nothing is skipped (nothing to clash with). Recommended: separate sub-account.
- Margin cap limits how much margin the arbitrage may use per exchange.

## Files (all under DATA_DIR/arb/, so they live on the Railway volume and are inside backup_data.sh)
- `settings.json` (the tab's settings), `keys.json` (API keys, Fernet-encrypted with the hub's master key; the browser only gets the last 4 characters)
- `state_<module>_<market>_<mode>.json` (open trades and mock wallets), `trades.csv` (journal), `risk_state.json` (today's live P&L), `STOP.txt` (kill switch).
- "Reset mock results" deletes mock/live-mock state and mock rows only. Live state and live rows are never touched.

## Behaviour to know
- Engines run in a worker thread after each scan; every exchange call goes through the shared `api_guard` (rate limits, 429/418 pauses).
- Scanner off while a live trade is open = nothing manages it (the tab shows a red banner). After a restart, saved open live trades are picked up again and only closed (no new entries), even if their module is switched off.
- One-leg fill: futures, the filled leg is closed at once; spot, held until the profit % (pair blocked while held). Telegram alert each time.
- Telegram: alerts for one-leg, stuck close (3 and 20 tries), unclear order, daily loss limit, old prices with a live trade open, unmanaged live trades, engine errors; plus an optional message on every trade opened/closed (setting "Telegram on every trade": off / live [default] / all).
- Daily loss counts CLOSED live trades only, per UTC day.
- Starts only after the storage check and the single-instance lock pass (same as the other engines). No new dependencies.

## API (all POST need login)
`GET /api/arb`, `POST /api/arb/settings` (body may carry `confirm_live`), `/api/arb/keys`, `/api/arb/keys/check` (read-only, sends no order), `/api/arb/stop`, `/api/arb/triangular`, `/api/arb/mock/reset`.

## Tests
`python3 -m unittest discover -s tests -t .` (1671 tests). Arbitrage: `tests/test_arb*.py` (fake exchanges, no network) and `tests/browser_arbitrage_check.py`.
