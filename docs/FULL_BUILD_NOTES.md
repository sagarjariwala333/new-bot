# Build notes (owner only) - Strategy Hub Bot

- The Strategy Hub is the only way a pair gets a strategy. No preset strategy, no starter values.
- The setup page and Add pair form hold pair-level settings only. All strategy settings are Hub boxes. The old Hull code is still in the files, unreachable (not deleted).
- Order-id prefixes `hullbot_` / `hullbotokx` are unchanged on purpose, so orders already on an exchange are still recognised.
- NOT proven on a real exchange: TP limit orders, trailing-stop moves, shadow mode. Test on Binance testnet / OKX demo first.
- The older Hull builds and their documents are in the earlier zips, not in this one.

## 2026-11-15 Arbitrage tab
Built inside the hub (`app/arb/`, 4 steps: scanner, mock, live, locks). See `docs/ARBITRAGE_FOR_DEV.md`. Untested on a real exchange: run `docs/SMOKE_TEST_ARBITRAGE.txt`.
