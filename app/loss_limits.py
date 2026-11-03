"""
loss_limits.py
==============

LOSS LIMIT (owner 2026-11-01): "stand alone per account per trade ... and also entire".

Three levels, each with an optional DAILY and an optional WEEKLY limit. A limit is an amount in USDT, or a % of the
account balance at the start of the day / week (owner 2026-11-02: "Have both so user can choose $ or %"):
  strategy : the Loss limit tab of a Strategy Hub plan (stored in the plan: plan["loss_limit"]) - counts the realized
             result of THAT pair (account + symbol)
  account  : one account, all its pairs together (set in the "Loss limits" box of the dashboard)
  entire   : every account on both platforms together (same box)

What it does: when the realized loss of the day / week (net result of everything closed in that period, wins count
against losses) has reached an amount, the Strategy Hub pairs in that scope make NO NEW entry until the period is over.
A % limit is worked out ONCE per day / week: % x the account balance at the start of that period (the balance the bot
saved at the first check after 00:00 SGT, Monday 00:00 for a week; for "all accounts together" the saved balances of the
accounts added up). From then on it is a fixed USDT number for the whole period, so it does not shrink while you lose.
If both a USDT amount and a % are keyed in for the same period, whichever is reached first stops the entries.
It never touches a trade that is already open: an open trade keeps its own stops, take profit, trailing stop and
flip rules (owner: "no trade should be distracted by external factors.. only its own price action"). An unrealized
(open) loss is not counted; only what has been closed.

Where the numbers come from: the ledger (app/ledger.py), the bot's own record of every closed trade and every part
closed (take profit fills, parts closed by hand), net of the real fees where they are known. Every pair of every
strategy is in it (also a Base V3 pair), so the account and entire levels see all of them. Only Strategy Hub pairs
are stopped from entering; Base V3 pairs are not gated here.

Days and weeks follow Singapore time (SGT, UTC+8): a day starts at 00:00, a week starts on Monday at 00:00.

The settings of the account and entire levels sit in one small file on the server (DATA_DIR/loss_limits.json, the
Railway volume, so they survive restarts and new zips). That file also remembers which "limit reached" emails were
sent, so one limit sends one email per period (not one per candle).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from app.fsutil import atomic_write
from app import ledger
from app.store import DATA_DIR

log = logging.getLogger("loss_limits")

LIMITS_FILE = DATA_DIR / "loss_limits.json"
SGT = timezone(timedelta(hours=8))
KINDS = ("daily", "weekly")
PCT_KEYS = ("daily_pct", "weekly_pct")
ALL_KEYS = KINDS + PCT_KEYS
KEEP_BASE_PERIODS = 3          # how many days / weeks of saved start balances are kept
KIND_TITLE = {"daily": "daily", "weekly": "weekly"}
SCOPE_TITLE = {"strategy": "this strategy", "account": "this account", "entire": "all accounts together"}
KEEP_SENT_DAYS = 21

_lock = threading.Lock()


class LossLimitError(ValueError):
    """Shown to the owner. status = HTTP status for the route."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- periods
def period_start(now: float, kind: str) -> float:
    """Start (epoch seconds) of the current SGT day, or of the current SGT week (Monday 00:00)."""
    d = datetime.fromtimestamp(now, SGT)
    day0 = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "weekly":
        day0 = day0 - timedelta(days=day0.weekday())
    return day0.timestamp()


def period_end(now: float, kind: str) -> float:
    start = datetime.fromtimestamp(period_start(now, kind), SGT)
    return (start + timedelta(days=7 if kind == "weekly" else 1)).timestamp()


def sgt_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, SGT).strftime("%a %d %b %H:%M SGT")


# ---------------------------------------------------------------- what was realized (from the ledger)
def _sum_file(path, since: float) -> float:
    total = 0.0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                pnl = row.get("pnl")
                closed = row.get("closed_at")
                if pnl is None or closed is None:
                    continue
                try:
                    if float(closed) >= since:
                        total += float(pnl)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return 0.0
    return total


def realized(scope: str, account_id: str | None, symbol: str | None, since: float) -> float:
    """Net result (USDT) of everything closed since `since`, for one pair, one account or everything."""
    d = ledger.LEDGER_DIR
    if scope == "strategy":
        return _sum_file(ledger._path(account_id, symbol), since)
    if scope == "account":
        return sum(_sum_file(p, since) for p in d.glob(f"{account_id}_*.jsonl"))
    return sum(_sum_file(p, since) for p in d.glob("*.jsonl"))


# ---------------------------------------------------------------- the settings of the account / entire levels
def _clean_amount(v, where: str, pct: bool = False):
    if v is None or v == "":
        return None
    if pct:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not (0.01 <= v <= 100):
            raise LossLimitError(400, f"{where}: key in a % from 0.01 to 100, or leave it empty")
        return float(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0.01:
        raise LossLimitError(400, f"{where}: key in an amount of at least 0.01 USDT, or leave it empty")
    return float(v)


def _clean_pair(d, where: str) -> dict:
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise LossLimitError(400, f"{where}: wrong format")
    return {k: _clean_amount(d.get(k), f"{where} {k.replace('_pct', ' %')}", pct=k in PCT_KEYS) for k in ALL_KEYS}


def _empty() -> dict:
    return {"entire": {k: None for k in ALL_KEYS}, "accounts": {}, "sent": {}, "base": {}}


def load() -> dict:
    with _lock:
        if not LIMITS_FILE.exists():
            return _empty()
        try:
            raw = json.loads(LIMITS_FILE.read_text())
        except Exception as e:
            log.error("Could not read %s (%s) - no account / entire loss limit is applied until it is fixed. "
                      "The file is left as it is.", LIMITS_FILE, e)
            raise LossLimitError(500, f"The loss limits file cannot be read ({e}). Nothing was changed.")
    out = _empty()
    if isinstance(raw, dict):
        ent = raw.get("entire")
        if isinstance(ent, dict):
            out["entire"] = {k: ent.get(k) for k in ALL_KEYS}
        accs = raw.get("accounts")
        if isinstance(accs, dict):
            out["accounts"] = {str(a): {k: (v or {}).get(k) for k in ALL_KEYS} for a, v in accs.items() if isinstance(v, dict)}
        sent = raw.get("sent")
        if isinstance(sent, dict):
            out["sent"] = sent
        base = raw.get("base")
        if isinstance(base, dict):
            out["base"] = {str(p): {str(a): float(v) for a, v in d.items()
                                    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
                           for p, d in base.items() if isinstance(d, dict)}
    return out


def _save(data: dict) -> None:
    with _lock:
        atomic_write(LIMITS_FILE, json.dumps(data), secret=False)


def save_settings(entire, accounts) -> dict:
    """Replaces the entire level (when given) and the listed accounts. What is not given is left as it is."""
    data = load()
    if entire is not None:
        data["entire"] = _clean_pair(entire, "All accounts together")
    if accounts is not None:
        if not isinstance(accounts, dict):
            raise LossLimitError(400, "accounts: wrong format")
        for acc_id, vals in accounts.items():
            pair = _clean_pair(vals, f"Account {acc_id}")
            if all(pair[k] is None for k in ALL_KEYS):
                data["accounts"].pop(str(acc_id), None)
            else:
                data["accounts"][str(acc_id)] = pair
    _save(data)
    return data


# ---------------------------------------------------------------- the balance at the start of a day / week (base of a % limit)
def _pkey(now: float, kind: str) -> str:
    return f"{'d' if kind == 'daily' else 'w'}:{int(period_start(now, kind))}"


def _pct_set(d) -> bool:
    return isinstance(d, dict) and any(d.get(k) for k in PCT_KEYS)


def _base(scope: str, account_id: str | None, data: dict, now: float, kind: str):
    """The saved start-of-period balance this level's % is taken of: the account's own (this strategy, this account),
    or all saved accounts added up (all accounts together). None = not saved yet."""
    d = data["base"].get(_pkey(now, kind), {})
    if scope == "entire":
        return sum(d.values()) if d else None
    return d.get(account_id)


def needs_balance(account_id: str, plan: dict | None) -> bool:
    """True when a % limit applies to this account (this strategy, this account or all accounts together)."""
    ll = (plan or {}).get("loss_limit")
    if isinstance(ll, dict) and ll.get("on") is True and _pct_set(ll):
        return True
    try:
        data = load()
    except LossLimitError:
        return False
    return _pct_set(data["entire"]) or _pct_set(data["accounts"].get(account_id))


def balance_due(account_id: str, plan: dict | None, now: float | None = None) -> bool:
    """True when a % limit applies and this account's balance for the current day or week is not saved yet."""
    now = time.time() if now is None else now
    if not needs_balance(account_id, plan):
        return False
    try:
        data = load()
    except LossLimitError:
        return False
    return any(account_id not in data["base"].get(_pkey(now, k), {}) for k in KINDS)


def record_balance(account_id: str, equity, now: float | None = None) -> None:
    """Saves the account balance at the start of the current day and week (the first time it is seen in that period).
    `equity` is the balance NOW; what the ledger shows as closed since the period started is taken back off, so a
    late first sight still gives the balance at the start. Deposits / withdrawals are not known to the bot."""
    now = time.time() if now is None else now
    if isinstance(equity, bool) or not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
        return
    data = load()
    changed = False
    for k in KINDS:
        d = data["base"].setdefault(_pkey(now, k), {})
        if account_id not in d:
            d[account_id] = float(equity) - realized("account", account_id, None, period_start(now, k))
            changed = True
    if not changed:
        return
    for prefix in ("d:", "w:"):
        keys = sorted((p for p in data["base"] if p.startswith(prefix)), key=lambda p: int(p[2:]))
        for old in keys[:-KEEP_BASE_PERIODS]:
            data["base"].pop(old, None)
    _save(data)


def status(now: float | None = None, account_ids: list | None = None) -> dict:
    """What the dashboard box shows: the amounts and what has been lost so far this day / week."""
    now = time.time() if now is None else now
    data = load()
    out = {"entire": {}, "accounts": {}, "day_start": sgt_text(period_start(now, "daily")),
           "week_start": sgt_text(period_start(now, "weekly"))}

    def one(scope, acc_id, lim):
        row = {}
        for k in KINDS:
            base = _base(scope, acc_id, data, now, k)
            pct = lim.get(k + "_pct")
            row[k] = {"limit": lim.get(k), "pct": pct, "base": base,
                      "pct_usdt": (base * pct / 100.0) if (pct and base and base > 0) else None,
                      "result": realized(scope, acc_id, None, period_start(now, k))}
        return row
    out["entire"] = one("entire", None, data["entire"])
    for acc_id in (account_ids if account_ids is not None else list(data["accounts"])):
        out["accounts"][acc_id] = one("account", acc_id, data["accounts"].get(acc_id, {}))
    return out


# ---------------------------------------------------------------- the check before a new entry
def check(account_id: str, symbol: str, plan: dict | None, now: float | None = None, equity=None) -> dict | None:
    """None = the entry may go ahead. Otherwise the first limit that has been reached:
    {"key", "scope", "kind", "limit", "lost", "until", "text"}. Order: this strategy, this account, all accounts.
    `equity` = the balance of this account now; it is only needed for a % limit when the start-of-period balance has
    not been saved yet. A % limit whose base is unknown cannot be checked and is left out (logged)."""
    now = time.time() if now is None else now
    levels = []
    ll = (plan or {}).get("loss_limit")
    if isinstance(ll, dict) and ll.get("on") is True:
        levels.append(("strategy", {k: ll.get(k) for k in ALL_KEYS}))
    try:
        data = load()
    except LossLimitError as e:
        # An unreadable file must not silently switch the protection off: say so in the log, keep the strategy level.
        log.error("Account / entire loss limits skipped: %s", e)
        data = _empty()
    levels.append(("account", data["accounts"].get(account_id) or {}))
    levels.append(("entire", data["entire"]))
    if equity is not None and any(_pct_set(a) for _, a in levels):
        try:
            record_balance(account_id, equity, now)
            data = load()
        except Exception as e:
            log.error("Could not save the start-of-period balance of %s (%s).", account_id, e)
    for scope, amounts in levels:
        for kind in KINDS:
            since = period_start(now, kind)
            result = None
            for unit in ("usdt", "pct"):
                base = pct = None
                if unit == "usdt":
                    limit = amounts.get(kind)
                else:
                    pct = amounts.get(kind + "_pct")
                    if not pct:
                        continue
                    base = _base(scope, account_id, data, now, kind)
                    if base is None or base <= 0:
                        log.warning("Loss limit %s %s %% of %s: the balance at the start of the period is not known, "
                                    "this limit cannot be checked now.", scope, kind, account_id)
                        continue
                    limit = base * float(pct) / 100.0
                if not limit:
                    continue
                if result is None:
                    result = realized(scope, account_id, symbol, since)
                if result <= -float(limit):
                    until = period_end(now, kind)
                    how = "" if unit == "usdt" else f" ({float(pct):g} % of the {base:,.2f} USDT balance at the start)"
                    return {
                        "key": f"{scope}:{account_id if scope != 'entire' else '-'}:{symbol if scope == 'strategy' else '-'}:"
                               f"{kind}{'%' if unit == 'pct' else ''}:{int(since)}",
                        "scope": scope, "kind": kind, "limit": float(limit), "lost": -result, "until": until,
                        "unit": unit,
                        "text": (f"LOSS LIMIT reached ({SCOPE_TITLE[scope]}, {KIND_TITLE[kind]}): lost {-result:,.2f} USDT "
                                 f"of the {float(limit):,.2f} USDT limit{how} since {sgt_text(since)}. No new entry until "
                                 f"{sgt_text(until)}. Open trades are not touched."),
                    }
    return None


def first_time(key: str, now: float | None = None) -> bool:
    """True the first time a reached limit is reported (so it sends one email per period). Remembered on disk."""
    now = time.time() if now is None else now
    try:
        data = load()
    except LossLimitError:
        return True
    sent = {k: v for k, v in data["sent"].items() if isinstance(v, (int, float)) and now - v < KEEP_SENT_DAYS * 86400}
    if key in sent:
        return False
    sent[key] = now
    data["sent"] = sent
    try:
        _save(data)
    except Exception as e:
        log.error("Could not remember the loss limit email %s (%s).", key, e)
    return True
