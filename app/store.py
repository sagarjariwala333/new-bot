"""
store.py
========

Persistent, encrypted-at-rest storage for accounts and pairs.

Layout on disk (data/accounts.json):
{
  "accounts": {
    "<account_id>": {
        "id": "...", "name": "...", "testnet": bool,
        "api_key_enc": "...", "api_secret_enc": "...",
        "pairs": {
            "BTCUSDT": { ...PairConfig fields... },
            ...
        }
    },
    ...
  }
}

API keys/secrets are encrypted with Fernet using a MASTER_ENC_KEY that lives
only in the environment (.env) - never in the JSON file itself, and the
plaintext key/secret are never returned by any API endpoint (always redacted
to "********" after the first save).

Hard limits enforced here: max 12 accounts, max 3 pairs per account.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from app.fsutil import atomic_write, harden_permissions

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(DATA_DIR, is_dir=True)
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

MAX_ACCOUNTS = 12
MAX_PAIRS_PER_ACCOUNT = 3


def _get_or_create_master_key() -> bytes:
    key = os.environ.get("MASTER_ENC_KEY")
    if key:
        return key.encode() if isinstance(key, str) else key
    # Fall back to a key file next to the data dir so restarts don't lose it
    # (still recommend setting MASTER_ENC_KEY in .env for production - a
    # backup that includes both accounts.json AND this key file can decrypt
    # the stored API credentials, so treat them as equally sensitive and
    # ideally keep the key out of the same backup set).
    key_file = DATA_DIR / ".master.key"
    if key_file.exists():
        return key_file.read_bytes()
    new_key = Fernet.generate_key()
    atomic_write(key_file, new_key.decode(), secret=True)
    return new_key


_fernet = Fernet(_get_or_create_master_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


# --------------------------------------------------------------------------
# Pair config - every strategy / sizing / risk / tracker / operational setting
# is blank (None) until the owner has entered it on the dashboard. There are
# deliberately NO default values here; see app/default_settings.py.
# --------------------------------------------------------------------------
@dataclass
class PairConfig:
    symbol: str
    enabled: bool = False           # whether the instance should be running
    timeframe: str | None = None

    # Strategy settings - mirror app/strategy.py StrategyParams 1:1 (plus the
    # operational ones below). All blank until entered.
    hma_length: int | None = None
    ema_filter_length: int | None = None     # entry filter
    ema_sizing_length: int | None = None     # sizing only - never blocks an entry
    atr_length: int | None = None            # TP1/TP2 tracking only

    use_hold_rule: bool | None = None
    adx_di_length: int | None = None
    adx_smoothing: int | None = None
    hold_adx_level: float | None = None

    trend_alloc_pct: float | None = None
    counter_alloc_pct: float | None = None
    leverage: float | None = None            # trend-aligned trades
    counter_leverage: float | None = None    # counter-trend trades
    isolated_margin: bool | None = None
    # A pair that has never traded waits for a fresh LONG<->SHORT reversal
    # before its FIRST trade (never mid-trend), when this is on.
    wait_first_reversal: bool | None = None

    stop_loss_pct_equity: float | None = None   # fixed stop = this % of balance at entry
    tp1_atr_mult: float | None = None           # tracking only
    tp2_atr_mult: float | None = None           # tracking only

    use_shadow: bool | None = None
    shadow_trades: int | None = None
    shadow_slippage_ticks: int | None = None
    tracker_start_balance: float | None = None  # tracker tab starting amount

    # Per-pair Freeze - a deliberate, persistent lock (survives restarts).
    # Purely a protection against accidental human clicks - no effect on the
    # bot's own behaviour. While True, every mutating action on this pair
    # (edit, start, stop, restart, delete) is refused until unfrozen.
    frozen: bool = False

    # Operational
    poll_seconds: int | None = None
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = None   # pause NEW entries (never exits) if the price feed goes quiet this long


def _clean_pair_dict(raw: dict, cls) -> dict:
    """Keeps only fields the dataclass knows (ignores anything else in the file)."""
    known = set(cls.__dataclass_fields__)
    return {k: v for k, v in raw.items() if k in known}


@dataclass
class AccountConfig:
    id: str
    name: str
    testnet: bool = False
    api_key_enc: str = ""
    api_secret_enc: str = ""
    max_account_exposure_pct: float | None = None   # None = no cap (cross-pair exposure lock)
    # Withdrawal alert (Telegram-only, no auto-withdrawal - matches the
    # reference Pine script's alertcondition(), which is itself just a
    # notification for a manual action, not an automated trade). Equity is
    # account-wide, not per-pair, so this lives here, not on PairConfig.
    withdraw_alert_enabled: bool = False
    withdraw_alert_threshold: float | None = None
    withdraw_alert_fired: bool = False              # latches True once crossed - never re-fires for the same crossing
    pairs: dict[str, PairConfig] = field(default_factory=dict)


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: dict[str, AccountConfig] = {}
        self._load()

    def _load(self):
        if not ACCOUNTS_FILE.exists():
            self._accounts = {}
            return
        raw = json.loads(ACCOUNTS_FILE.read_text())
        accounts = {}
        for acc_id, acc in raw.get("accounts", {}).items():
            pairs = {}
            for sym, pdict in acc.get("pairs", {}).items():
                pairs[sym] = PairConfig(**_clean_pair_dict(pdict, PairConfig))
            acc = {**acc, "pairs": pairs}
            accounts[acc_id] = AccountConfig(**acc)
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
        atomic_write(ACCOUNTS_FILE, json.dumps(payload, indent=2), secret=True)

    # ---------------------------------------------------------------- accounts
    def list_accounts(self) -> list[AccountConfig]:
        with self._lock:
            return list(self._accounts.values())

    def get_account(self, account_id: str) -> Optional[AccountConfig]:
        with self._lock:
            return self._accounts.get(account_id)

    def create_account(self, name: str, api_key: str, api_secret: str, testnet: bool) -> AccountConfig:
        with self._lock:
            if len(self._accounts) >= MAX_ACCOUNTS:
                raise ValueError(f"Maximum of {MAX_ACCOUNTS} accounts already configured")
            acc_id = uuid.uuid4().hex[:12]
            acc = AccountConfig(
                id=acc_id, name=name, testnet=testnet,
                api_key_enc=encrypt(api_key), api_secret_enc=encrypt(api_secret),
                pairs={},
            )
            self._accounts[acc_id] = acc
            self._save()
            return acc

    def update_account(self, account_id: str, name: str | None = None,
                        api_key: str | None = None, api_secret: str | None = None,
                        testnet: bool | None = None,
                        max_account_exposure_pct: float | None = "unset",
                        withdraw_alert_enabled: bool | None = None,
                        withdraw_alert_threshold: float | None = "unset") -> AccountConfig:
        with self._lock:
            acc = self._accounts[account_id]
            if name is not None:
                acc.name = name
            if api_key:
                acc.api_key_enc = encrypt(api_key)
            if api_secret:
                acc.api_secret_enc = encrypt(api_secret)
            if testnet is not None:
                acc.testnet = testnet
            if max_account_exposure_pct != "unset":
                acc.max_account_exposure_pct = max_account_exposure_pct
            if withdraw_alert_enabled is not None:
                acc.withdraw_alert_enabled = withdraw_alert_enabled
            if withdraw_alert_threshold != "unset":
                acc.withdraw_alert_threshold = withdraw_alert_threshold
                # Changing the threshold (or re-saving it) means "watch for
                # this again" - reset the latch so a new/updated threshold
                # isn't silently treated as already-fired.
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

    def get_credentials(self, account_id: str) -> tuple[str, str]:
        acc = self._accounts[account_id]
        return decrypt(acc.api_key_enc), decrypt(acc.api_secret_enc)

    # ---------------------------------------------------------------- pairs
    def add_pair(self, account_id: str, pair: PairConfig) -> PairConfig:
        with self._lock:
            acc = self._accounts[account_id]
            if len(acc.pairs) >= MAX_PAIRS_PER_ACCOUNT:
                raise ValueError(f"Maximum of {MAX_PAIRS_PER_ACCOUNT} pairs per account")
            if pair.symbol in acc.pairs:
                raise ValueError(f"{pair.symbol} already exists on this account")
            acc.pairs[pair.symbol] = pair
            self._save()
            return pair

    def update_pair(self, account_id: str, symbol: str, updates: dict) -> PairConfig:
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
        """2026-09-19, owner request - a dedicated method (not routed
        through the general update_pair() a config edit uses), matching
        the same reasoning as set_pair_enabled above - this is a distinct
        action with its own dedicated API endpoint and its own
        confirmation step on the dashboard, not a field bundled into a
        general settings edit."""
        return self.update_pair(account_id, symbol, {"frozen": frozen})


store = Store()
