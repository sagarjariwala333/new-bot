#!/usr/bin/env bash
#
# smoke_test.sh - closes item 15 as honestly as this project can: a real,
# dependency-complete boot test against the ACTUAL fastapi/aiohttp/
# pydantic/uvicorn packages, not the test stubs.
#
# Why this has to be a script for YOU to run, not something already done
# here: this project's own dev sandbox has no PyPI access at all (confirmed
# directly - `pip install fastapi` returns "No matching distribution
# found", not a permissions or proxy error). Every test in this project
# that touches app.main/app.api runs against hand-written stubs standing in
# for those packages (see tests/_fastapi_stub.py, _aiohttp_stub.py,
# _pydantic_stub.py) - they prove the code is structurally sound, but they
# cannot prove the real packages behave identically. This script is the
# one remaining verification gap that genuinely cannot be closed with more
# code - only by actually running it in an environment with real internet
# access.
#
# ─── Usage ───────────────────────────────────────────────────────────────
#   cd hull_futures_bot
#   python3 -m venv venv
#   source venv/bin/activate
#   pip install -r requirements.txt
#   ./deploy/smoke_test.sh
#
# A clean run means: the real FastAPI app imports without error, every
# expected route is registered, the full test suite passes against the
# real packages (not stubs), and a real running server actually responds
# on /healthz. That's as close to "verified" as this can get before you
# also do the real exchange testnet/demo checks listed in README.md
# (which this script does NOT cover - this is purely about the
# web server/dependency layer, not exchange connectivity).

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== 1/4: importing app.main with the REAL fastapi/aiohttp/pydantic ==="
python3 -c "
import app.main
routes = [r.path for r in app.main.app.routes]
assert '/healthz' in routes, 'expected /healthz route missing'
assert '/' in routes, 'expected dashboard route missing'
print(f'OK - {len(routes)} routes registered, including /healthz and /')
"

echo ""
echo "=== 2/4: running the full test suite against the REAL packages (not the stubs) ==="
python3 -m unittest discover -s tests -p "test_*.py"

echo ""
echo "=== 3/4: starting the real server and hitting /healthz ==="
if ! command -v curl &> /dev/null; then
    echo "SKIPPED - curl not found; start the server manually with 'python3 run.py' and check "
    echo "http://127.0.0.1:${PORT:-8000}/healthz in a browser instead."
else
    python3 run.py &
    SERVER_PID=$!
    trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
    sleep 2
    if curl -sf "http://127.0.0.1:${PORT:-8000}/healthz" > /dev/null; then
        echo "OK - /healthz responded on a real running server"
    else
        echo "FAILED - could not reach /healthz on a real running server" >&2
        exit 1
    fi
    kill "$SERVER_PID" 2>/dev/null || true
    trap - EXIT
fi

echo ""
echo "=== 4/4: summary ==="
echo "All checks passed against your REAL environment - this closes the one"
echo "verification gap that could not be completed in the sandbox this"
echo "project was built in. This does NOT cover real Binance connectivity -"
echo "see README.md for the testnet/demo steps still needed."
