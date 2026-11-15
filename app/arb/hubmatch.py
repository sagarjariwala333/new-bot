"""Same account as the hub (owner decision 2026-11-15, option 2): the arbitrage skips every coin the hub trades on
that account. How it knows: the arbitrage key is compared with the keys of the hub's accounts. If it is the same
key, every pair configured on that hub account is off limits for the arbitrage FUTURES legs.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("arb.hubmatch")
TTL = 15.0
QUOTES = ("USDT", "USDC", "BUSD")


def base_of(symbol: str) -> str:
    if "-" in symbol:                                  # OKX: BTC-USDT-SWAP
        return symbol.split("-")[0].upper()
    s = symbol.upper()
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


class HubPairs:
    """{exchange: {base: account name}} for the hub accounts that share the given arbitrage key."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._at = float("-inf")
        self._cache = {}
        self._key = None

    def _hub_accounts(self, ex):
        """[(account name, api_key, [pair symbols])] from the hub's own stores."""
        out = []
        if ex == "binance":
            from app.store import store
            for acc in store.list_accounts():
                try:
                    out.append((acc.name, store.get_credentials(acc.id)[0], list(acc.pairs)))
                except Exception:
                    log.warning("could not read hub account %s", getattr(acc, "name", "?"))
        else:
            from app.okx_store import okx_store
            for acc in okx_store.list_accounts():
                try:
                    out.append((acc.name, okx_store.get_credentials(acc.id)[0], list(acc.pairs)))
                except Exception:
                    log.warning("could not read hub OKX account %s", getattr(acc, "name", "?"))
        return out

    def matches(self, ex: str, api_key: str) -> dict:
        """{base: account name} - all coins the hub trades on accounts that use this same key."""
        if not api_key:
            return {}
        cache_key = (ex, api_key)
        if self._key == cache_key and self._clock() - self._at < TTL:
            return self._cache
        out = {}
        try:
            for name, key, pairs in self._hub_accounts(ex):
                if key == api_key:
                    for sym in pairs:
                        out[base_of(sym)] = name
        except Exception:
            log.exception("hub account lookup failed")
            raise
        self._key, self._at, self._cache = cache_key, self._clock(), out
        return out

    def accounts_using(self, ex: str, api_key: str) -> list:
        """Names of the hub accounts that use this exact key (for the tab's information line)."""
        if not api_key:
            return []
        try:
            return [name for name, key, _ in self._hub_accounts(ex) if key == api_key]
        except Exception:
            return []
