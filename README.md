# Strategy Hub Bot - OWNER BUILD

Binance and OKX USDⓈ-M perpetual futures bot, run from one web dashboard. You build the strategy for each pair
yourself in the **Strategy Hub** tab. There is **no preset strategy and no starter value**: a first install is
entirely blank, and a pair with no saved plan does not start.

**Owner only.** Keep this package private.

Nothing in this package starts trading by itself.

## Who does what

| Person | Does |
|---|---|
| **IT person (if any)** | Deploys on the owner's Railway project with `docs/RAILWAY_SETUP.md`, then leaves the project. |
| **Owner** | Types every secret (dashboard password, encryption key, exchange API keys, Telegram/SMTP) into Railway Variables and the dashboard, fills in the Default Settings and each pair's plan, tests, and decides when to turn live trading on. |

## Project layout

```
app/
  strategy_hub.py      the plan: indicators, flip, wait, sizing, TP, trailing, shadow (pure calculations)
  hub_trading.py       how a pair with a plan trades (flip order, entry, TP, trailing, shadow)
  hub_api.py / hub_state.py   saving a plan, per-pair hub memory
  default_settings.py  blank defaults, validation, Confirm step, alert email, live-trading switch
  strategy.py / base_v3_tracker.py   shared calculations, tracker + shadow state (older code the hub reuses)
  instance.py          one live loop per account+pair: orders, stop, recovery
  manager.py / okx_manager.py   run all Binance / OKX pairs (the start gates live here)
  binance_futures.py / okx_futures.py   exchange adapters
  store.py / okx_store.py       encrypted account + pair settings
  api/                 dashboard API (FastAPI)
  static/dashboard.html
  ledger.py, reconciliation.py, risk_guard.py, singleton_lock.py, ...
deploy/                optional VPS files (systemd, backup, smoke test) - NOT needed on Railway
docs/RAILWAY_SETUP.md  step-by-step deployment
docs/HANDOFF_READ_FIRST.md  rules and open items, for a new chat
railway.json           Railway build/deploy/health-check settings
tests/                 unittest suite
```

## Run locally (optional)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill in the secrets; set HOST=127.0.0.1 for local use
python run.py
```

Open http://localhost:8000 and log in.

## Using the dashboard (owner)

1. **Default Settings** tab: everything starts BLANK. Fill the pair-level settings for Binance and/or OKX, press
   **Confirm Default Settings**, review the summary, confirm. Also set the alert email here.
2. **Add Account**: Binance or OKX tab. Keys are encrypted at rest and never shown again.
   Use a testnet / demo account first.
3. **Add Pair** (needs the confirmed defaults). A new pair does NOT start: it has no strategy yet.
4. **Strategy Hub** tab (Layout 2, 2026-10-23): name the strategy and choose the account. The menu on the left lists
   8 functions: Pair & chart, Indicators (entry / flip-exit ticks and their numbers), Combine & flip plus hold rule
   (ALL / ANY / At least N), Entry (entry wait with a warning above 0, size: allocation % or a fixed base amount that
   grows with the last closed total, optional lock-out %, leverage, email alert on margin), Max loss (% of balance,
   % of entry amount, or fixed $), Stop loss (trailing), Take profit, Shadow mode. The first four are needed by every
   strategy; pick the others, press Confirm selection, then open each function and fill it in (nothing is
   pre-filled; each box has a short explanation beside it). **Save** keeps the plan. **Execute** saves it and starts the
   pair (it asks you to confirm first). The pair must already be added to the account.
5. **Start / Stop / Freeze** each pair. A pair with no saved plan will not start. **STOP ALL / START ALL** cover both exchanges.
6. **Log**, **Ledger** (real trades, CSV), **Tracker**, **Analysis**.
7. **Live trading switch** (Default Settings tab): OFF by default. Turn it on only when you are ready for
   real-money accounts to trade.

A plan saved while the pair holds a trade starts after the trade closes. A flip is: the candle close gives the
signal, CLOSE, FLAT, CHECK FOR CHANGES IN RULES, otherwise OPEN THE OPPOSITE.

## Safety features

Client-order-id tagging, ambiguous-order recovery, duplicate protection, filled-quantity confirmation,
unprotected-position recovery with emergency flatten, idempotent close, pending reconciliation,
exposure cap, leverage-bracket check, stale-feed entry pause, consecutive-failure stop, atomic
single-instance lock, restart recovery, Telegram + email alerts.

## Tests

```bash
python -m unittest discover -s tests -t .
```

The suite uses stand-ins for `fastapi`, `pydantic` and `aiohttp` when the real packages are not installed.
Run `deploy/smoke_test.sh` in an environment with internet access to run the suite against the real packages.
The tests use placeholder numbers that are NOT the owner's settings (see `tests/_test_values.py`).

## Security

- API secrets are encrypted with Fernet (`MASTER_ENC_KEY`, or `data/.master.key` - back it up).
- The dashboard needs a login (PBKDF2, sessions, lockout, CSRF). Do not expose it without HTTPS.
- The interactive API docs (`/docs`, `/redoc`) and `/openapi.json` are switched off; they would otherwise be readable by anyone without a login. `/healthz` stays open on purpose (uptime monitors) and shows no account data.
- Settings the owner enters are stored in `DATA_DIR/default_settings.json` and in the pair records on the
  server's volume. Anyone who can log in to the dashboard, open a shell on the service, or read the volume
  can see them. The owner should remove the IT person from the Railway project before entering them.
- Start every new pair on testnet / demo first.
