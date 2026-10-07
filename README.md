# Futures Bot — "For Dev" package

Binance and OKX USDⓈ-M perpetual futures bot, run from one web dashboard.

**This package contains NO trading settings.** Every indicator length, level,
allocation, leverage, stop and tracker number, and every on/off strategy switch, is
**blank**. The owner types them into the dashboard (**Default Settings** tab) after
deployment and presses **Confirm Default Settings**. Until then:

- no new pair can be created (the server refuses it),
- no pair with a blank setting can be started (the server refuses it),
- real-money accounts cannot start at all while the **Live trading** switch is OFF
  (it is OFF by default; Binance testnet / OKX demo accounts are not affected).

Nothing in this package starts trading by itself.

## Who does what

| Person | Does |
|---|---|
| **IT person** | Deploys the app on the owner's Railway project using `docs/RAILWAY_SETUP.md`, then leaves the project. |
| **Owner** | Types every secret (dashboard password, encryption key, exchange API keys, Telegram/SMTP) into Railway Variables and the dashboard, fills in the Default Settings, tests, and decides when to turn live trading on. |

## Project layout

```
app/
  default_settings.py  blank defaults, validation, Confirm step, alert email, live-trading switch
  strategy.py          strategy calculations (pure, exchange-neutral)
  base_v3_tracker.py   tracker + shadow state + last processed candle (persisted)
  instance.py          one live loop per account+pair: orders, stop, recovery
  manager.py / okx_manager.py   run all Binance / OKX pairs (the start gates live here)
  binance_futures.py / okx_futures.py   exchange adapters
  store.py / okx_store.py       encrypted account + pair settings
  api/                 dashboard API (FastAPI)
  static/dashboard.html
  ledger.py, reconciliation.py, risk_guard.py, singleton_lock.py, ...
deploy/                optional VPS files (systemd, backup, smoke test) - NOT needed on Railway
docs/RAILWAY_SETUP.md  step-by-step deployment for the IT person
railway.json           Railway build/deploy/health-check settings
.env.example           environment variable template (no values)
tests/                 unittest suite (infrastructure + the blank-settings rules)
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

1. **Default Settings** tab: fill in every field for Binance and/or OKX (all start blank),
   press **Confirm Default Settings**, review the summary, confirm. Also set the alert email here.
2. **Add Account**: Binance or OKX tab. Keys are encrypted at rest and never shown again.
   Use a testnet / demo account first.
3. **Add Pair**: the form is pre-filled from your confirmed defaults and each value can be changed per pair.
   Editing a pair with an open trade applies once it is flat.
4. **Start / Stop / Freeze** each pair. **STOP ALL / START ALL** cover both exchanges.
5. **Log**, **Ledger** (real trades, CSV), **Tracker**, **Analysis** (offline walk-forward / Monte Carlo replay;
   its starting equity and cost numbers are also typed in by you).
6. **Live trading switch** (Default Settings tab): OFF by default. Turn it on only when you are ready for
   real-money accounts to trade.

Changing a default later marks that platform *unconfirmed* again, which blocks only the creation of NEW pairs
until you press Confirm again. Pairs that already exist are not touched.

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
