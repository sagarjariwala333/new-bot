# Strategy Hub Bot

Automated multi-exchange trading bot and web dashboard for **Binance** and **OKX** USDⓈ-M perpetual futures.

Built with a modular rule-builder engine (**Strategy Hub**), allowing fine-grained customization of indicators, combine/hold logic, entry sizing, multi-stage Take Profit, dynamic trailing stops, and shadow mode.

> **Repository:** [https://github.com/sagarjariwala333/new-bot.git](https://github.com/sagarjariwala333/new-bot.git)

---

## 🌟 Key Features

- **Multi-Exchange Execution:** Native async integration with Binance USDⓈ-M Futures and OKX Perpetual Swaps.
- **Strategy Hub UI:** Visual strategy builder with custom indicators (EMA, RSI, MACD, Supertrend, ATR, Bollinger Bands, Support & Resistance, etc.).
- **Risk & Safety Protections:**
  - Client-order-id tracking and duplicate order protection.
  - Ambiguous-order recovery and filled-quantity confirmation.
  - Unprotected position detection with emergency flatten safeguard.
  - Auto leverage-bracket checking and exposure caps.
  - Stale-feed entry suspension and consecutive-failure auto-stop.
  - Single-instance atomic locking.
- **Encrypted Secrets:** Zero plaintext credentials; exchange API keys are encrypted at rest using Fernet encryption (`MASTER_ENC_KEY`).
- **Arbitrage Module (Beta):** Integrated cross-exchange spatial arbitrage engine (`docs/ARBITRAGE_FOR_DEV.md`).
- **Telegram & SMTP Alerts:** Real-time push notifications for executions, margin warnings, and system health alerts.

---

## 📁 Project Architecture

```
strategy_hub_bot/
├── app/
│   ├── api/               # FastAPI route controllers (auth, dashboard, hub, settings, arb)
│   ├── arb/               # Cross-exchange spatial arbitrage engine & scanner
│   ├── static/            # Frontend dashboard UI (HTML/CSS/JS)
│   ├── strategy_hub.py    # Indicator math, signal calculation, trailing/SL/TP logic
│   ├── hub_trading.py     # Execution engine translating hub plans to live exchange orders
│   ├── binance_futures.py # Binance USDⓈ-M API adapter
│   ├── okx_futures.py     # OKX Perpetual Swaps API adapter
│   ├── store.py / okx_store.py  # Encrypted configuration & account persistence
│   ├── risk_guard.py      # Circuit breakers, drawdown control, and exposure limits
│   └── instance.py        # Per-pair execution loop & state reconciliation
├── data/                  # Runtime persistence (ledger, tracker, positions, keys)
├── deploy/                # Deployment configurations (Systemd unit, backup scripts, smoke tests)
├── docs/                  # In-depth architectural guides & Railway setup docs
├── pine/                  # Reference TradingView Pine Script templates
├── tests/                 # Comprehensive test suite
├── .env.example           # Environment template
├── railway.json           # Railway cloud deployment configuration
├── requirements.txt       # Core project dependencies
└── run.py                 # Application launcher entry point
```

---

## 🚀 Quick Start

### 1. Clone & Set Up Environment

```bash
git clone https://github.com/sagarjariwala333/new-bot.git
cd new-bot
# or cd strategy_hub_bot
```

Create a virtual environment (Python 3.11 or 3.12 recommended):

```bash
python -m venv venv

# On Linux/macOS:
source venv/bin/activate

# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your parameters:
- `HOST`: Set to `127.0.0.1` for local runs, or `0.0.0.0` for cloud deployment.
- `PORT`: Web port (default `8000`).
- `DATA_DIR`: Path to data directory (e.g. `./data` locally or `/data` for Railway volume mounts).
- `MASTER_ENC_KEY`: Fernet encryption key used to protect API keys. Generate via:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `DASHBOARD_USERNAME` & `DASHBOARD_PASSWORD`: Initial login credentials for the admin dashboard.

### 3. Launch the Application

```bash
python run.py
```

Open your browser and navigate to:
```
http://127.0.0.1:8000
```

---

## 🛠️ Usage Workflow

1. **Configure Default Settings:** Navigate to the **Default Settings** tab. Review pair-level risk boundaries, max loss rules, and confirm defaults.
2. **Add Exchange Account:** In Binance or OKX tabs, submit your API credentials. Keys are encrypted immediately and never logged or exposed. *Start with Testnet/Demo accounts first.*
3. **Configure Strategy in Strategy Hub:**
   - Select pair & timeframe.
   - Configure technical indicator rules, holding requirements, and entry conditions.
   - Define multi-target Take Profits and dynamic ATR/Swing Trailing stops.
   - Click **Save** to store or **Execute** to activate for trading.
4. **Live Trading Toggle:** Live trading is disabled by default. Enable the switch in Default Settings once you're satisfied with your dry-run / testnet tests.

---

## ☁️ Deployment (Railway)

1. Connect your repository to [Railway](https://railway.com).
2. Attach a **Railway Volume** mounted at `/data` to persist trade journals, positions, and keys.
3. In service variables, set:
   - `HOST=0.0.0.0`
   - `DATA_DIR=/data`
   - `COOKIE_SECURE=true`
   - `MASTER_ENC_KEY=<generated-key>`
   - `DASHBOARD_USERNAME` & `DASHBOARD_PASSWORD`
4. Deploy using the provided [railway.json](file:///E:/21.11.2026_1546_Strategy_Hub_V-21.11.2026.1/strategy_hub_bot/railway.json). The service automatically uses healthchecks on `/healthz`.
5. For full instructions, refer to `docs/RAILWAY_SETUP.md`.

---

## 🧪 Testing

Run test discovery across all unit tests:

```bash
python -m unittest discover -s tests -t .
```

To run individual test modules (e.g. strategy hub logic):

```bash
python -m unittest tests.test_strategy_hub
```

---

## 🔒 Security Notice

- Never commit `.env` or sensitive `.key` / `.json` files from `data/` to version control.
- Ensure HTTPS/TLS termination is enabled in public production environments.
- API endpoints for interactive OpenAPI (`/docs`, `/redoc`) are disabled by default in production.
