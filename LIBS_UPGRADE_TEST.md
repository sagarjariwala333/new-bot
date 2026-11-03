# v1.3-LIBS - what to test (for the developer)

This copy is identical to v1.3 except `requirements.txt`: the web and connection libraries are raised to
security-fixed versions. It has NOT been run against those versions by the person who built it.

## Step 1 - install and run the built-in check (about 5 minutes)
```bash
cd strategy_hub_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./deploy/smoke_test.sh
```
Pass = it ends without errors. It imports the real app, runs the whole test suite against the REAL packages
(not the stand-ins), starts the server and checks /healthz.

If `pip install` fails with a version conflict, STOP and send the full error text. Do not edit versions by hand.

## Step 2 - send back
1. The full output of Step 1.
2. The output of `pip freeze`.

## Step 3 - testnet run (only if Step 1 passed)
Deploy this copy to the TEST Railway service and repeat the usual testnet checks:
- /healthz says OK, the dashboard loads and you can log in.
- Binance testnet connection works (no new errors in the log).
- OKX demo connection works.
- A trade opens, its stop order appears, and the trade closes.

## If anything fails
Go back to v1.3. Nothing about the strategy changed, so v1.3 remains the safe fallback.
