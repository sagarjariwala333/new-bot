"""
okx_store.py
=============

Persistent, encrypted-at-rest storage for OKX accounts and pairs - the OKX
tab's own store, deliberately separate from app/store.py (Binance's store),
per the owner's explicit decision: "these two are different platforms, they
should have different requirements, each for their own tabs." Nothing here
is shared with Binance's account list except the master encryption key
(reused from app.store so this project manages exactly one secret, not two).

Layout on disk (data/okx_accounts.json):
{
  "accounts": {
    "<account_id>": {
        "id": "...", "name": "...", "demo": bool, "sub_account_label": "...",
        "api_key_enc": "...", "api_secret_enc": "...", "passphrase_enc": "...",
        "pairs": { "BTC-USDT-SWAP": { ...OKXPairConfig fields... }, ... }
    },
    ...
  }
}

Hard limits enforced here, per OKX's own account/sub-account rules
(confirmed 2026-09-14 - see this session's research): max 5 OKX accounts
(a "standard"/regular user's sub-account cap), max 5 pairs per account.

`sub_account_label` is purely informational (a note to the operator about
which OKX sub-account these credentials belong to) - this bot does NOT
create or manage OKX sub-accounts via API; that's a manual step on OKX's
website, same as the owner described.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.fsutil import atomic_write
from app.store import DATA_DIR, encrypt, decrypt  # reuse the one master key - see module docstring
from app.store import _clean_pair_dict

OKX_ACCOUNTS_FILE = DATA_DIR / "okx_accounts.json"

MAX_OKX_ACCOUNTS = 5           # OKX regular/standard-user sub-account cap
MAX_PAIRS_PER_OKX_ACCOUNT = 5  # per the owner's hard requirement 3


@dataclass
class OKXPairConfig:
    symbol: str                     # OKX instId format, e.g. "BTC-USDT-SWAP" - NOT Binance's "BTCUSDT"
    enabled: bool = False
    # Candle timeframe - blank until entered. OKX publishes only native
    # candles (see okx_futures.py OKX_BAR_MAP); stored in the same "12h"
    # style as Binance, the adapter sends OKX's UTC-aligned bar code.
    timeframe: str | None = None

    # Strategy settings - mirror app/strategy.py StrategyParams 1:1 (plus the
    # operational ones below). All blank until entered; see store.PairConfig.
    hma_length: int | None = None
    ema_filter_length: int | None = None
    ema_sizing_length: int | None = None
    atr_length: int | None = None

    use_hold_rule: bool | None = None
    adx_di_length: int | None = None
    adx_smoothing: int | None = None
    hold_adx_level: float | None = None

    trend_alloc_pct: float | None = None
    counter_alloc_pct: float | None = None
    leverage: float | None = None
    counter_leverage: float | None = None
    isolated_margin: bool | None = None
    wait_first_reversal: bool | None = None   # see store.PairConfig

    stop_loss_pct_equity: float | None = None
    tp1_atr_mult: float | None = None
    tp2_atr_mult: float | None = None

    use_shadow: bool | None = None
    shadow_trades: int | None = None
    shadow_slippage_ticks: int | None = None
    tracker_start_balance: float | None = None

    # See store.py's PairConfig.frozen.
    frozen: bool = False

    # Operational
    poll_seconds: int | None = None
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = None
    # OKX-specific: which price OKX's stop trigger watches (blank until entered).
    trigger_px_type: str | None = None


@dataclass
class OKXAccountConfig:
    id: str
    name: str
    demo: bool = False              # OKX's x-simulated-trading flag - equivalent of Binance's testnet
    sub_account_label: str = ""     # informational only - see module docstring
    api_key_enc: str = ""
    api_secret_enc: str = ""
    passphrase_enc: str = ""        # OKX-only third credential - Binance has no equivalent
    max_account_exposure_pct: float | None = None   # SEPARATE cap from any Binance account's - per owner's decision
    withdraw_alert_enabled: bool = False
    withdraw_alert_threshold: float | None = None
    withdraw_alert_fired: bool = False
    pairs: dict[str, OKXPairConfig] = field(default_factory=dict)


class OKXStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: dict[str, OKXAccountConfig] = {}
        self._load()

    def _load(self):
        if not OKX_ACCOUNTS_FILE.exists():
            self._accounts = {}
            return
        raw = json.loads(OKX_ACCOUNTS_FILE.read_text())
        accounts = {}
        for acc_id, acc in raw.get("accounts", {}).items():
            pairs = {}
            for sym, pdict in acc.get("pairs", {}).items():
                pairs[sym] = OKXPairConfig(**_clean_pair_dict(pdict, OKXPairConfig))
            acc = {**acc, "pairs": pairs}
            accounts[acc_id] = OKXAccountConfig(**acc)
        self._accounts = accounts

    def _save(self):
        payload = {
            "accounts": {
                acc_id: {
                    **{k: v for k, v in asdict(acc).items() if k != "pairs"},
                    "pairs": {sym: asdict(pc) for sym, pc in acc.pairs.items()},
                }
                for acc_id, acc in self._accounts.items()
            }
        }
        atomic_write(OKX_ACCOUNTS_FILE, json.dumps(payload, indent=2), secret=True)

    # ---------------------------------------------------------------- accounts
    def list_accounts(self) -> list[OKXAccountConfig]:
        with self._lock:
            return list(self._accounts.values())

    def get_account(self, account_id: str) -> Optional[OKXAccountConfig]:
        with self._lock:
            return self._accounts.get(account_id)

    def create_account(self, name: str, api_key: str, api_secret: str, passphrase: str,
                        demo: bool, sub_account_label: str = "") -> OKXAccountConfig:
        with self._lock:
            if len(self._accounts) >= MAX_OKX_ACCOUNTS:
                raise ValueError(f"Maximum of {MAX_OKX_ACCOUNTS} OKX accounts already configured "
                                  f"(OKX's own regular-user sub-account cap)")
            acc_id = uuid.uuid4().hex[:12]
            acc = OKXAccountConfig(
                id=acc_id, name=name, demo=demo, sub_account_label=sub_account_label,
                api_key_enc=encrypt(api_key), api_secret_enc=encrypt(api_secret),
                passphrase_enc=encrypt(passphrase), pairs={},
            )
            self._accounts[acc_id] = acc
            self._save()
            return acc

    def update_account(self, account_id: str, name: str | None = None,
                        api_key: str | None = None, api_secret: str | None = None,
                        passphrase: str | None = None, demo: bool | None = None,
                        sub_account_label: str | None = None,
                        max_account_exposure_pct: float | None = "unset",
                        withdraw_alert_enabled: bool | None = None,
                        withdraw_alert_threshold: float | None = "unset") -> OKXAccountConfig:
        with self._lock:
            acc = self._accounts[account_id]
            if name is not None:
                acc.name = name
            if api_key:
                acc.api_key_enc = encrypt(api_key)
            if api_secret:
                acc.api_secret_enc = encrypt(api_secret)
            if passphrase:
                acc.passphrase_enc = encrypt(passphrase)
            if demo is not None:
                acc.demo = demo
            if sub_account_label is not None:
                acc.sub_account_label = sub_account_label
            if max_account_exposure_pct != "unset":
                acc.max_account_exposure_pct = max_account_exposure_pct
            if withdraw_alert_enabled is not None:
                acc.withdraw_alert_enabled = withdraw_alert_enabled
            if withdraw_alert_threshold != "unset":
                acc.withdraw_alert_threshold = withdraw_alert_threshold
                acc.withdraw_alert_fired = False
            self._save()
            return acc

    def mark_withdraw_alert_fired(self, account_id: str):
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc:
                acc.withdraw_alert_fired = True
                self._save()

    def delete_account(self, account_id: str):
        with self._lock:
            self._accounts.pop(account_id, None)
            self._save()

    def get_credentials(self, account_id: str) -> tuple[str, str, str]:
        """Returns (api_key, api_secret, passphrase) - three values, not
        Binance's two, since OKX requires the passphrase on every signed
        request."""
        acc = self._accounts[account_id]
        return decrypt(acc.api_key_enc), decrypt(acc.api_secret_enc), decrypt(acc.passphrase_enc)

    # ---------------------------------------------------------------- pairs
    def add_pair(self, account_id: str, pair: OKXPairConfig) -> OKXPairConfig:
        with self._lock:
            acc = self._accounts[account_id]
            if len(acc.pairs) >= MAX_PAIRS_PER_OKX_ACCOUNT:
                raise ValueError(f"Maximum of {MAX_PAIRS_PER_OKX_ACCOUNT} pairs per OKX account "
                                  f"(hard requirement 3)")
            if pair.symbol in acc.pairs:
                raise ValueError(f"{pair.symbol} already exists on this account")
            acc.pairs[pair.symbol] = pair
            self._save()
            return pair

    def update_pair(self, account_id: str, symbol: str, updates: dict) -> OKXPairConfig:
        with self._lock:
            acc = self._accounts[account_id]
            pc = acc.pairs[symbol]
            for k, v in updates.items():
                if hasattr(pc, k):
                    setattr(pc, k, v)
            self._save()
            return pc

    def delete_pair(self, account_id: str, symbol: str):
        with self._lock:
            acc = self._accounts[account_id]
            acc.pairs.pop(symbol, None)
            self._save()

    def set_pair_enabled(self, account_id: str, symbol: str, enabled: bool):
        self.update_pair(account_id, symbol, {"enabled": enabled})

    def set_pair_frozen(self, account_id: str, symbol: str, frozen: bool):
        """See store.py's identical method for the full reasoning."""
        return self.update_pair(account_id, symbol, {"frozen": frozen})


okx_store = OKXStore()
