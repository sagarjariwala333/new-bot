"""API-ban protection for Binance and OKX (owner 2026-11-11).

ONE guard per exchange, shared by EVERY account and pair in this process (Binance counts request weight per IP,
not per account, so all pairs share one budget).

What it does
  * Counts the weight of every request in a sliding 60 s window and also trusts the number the exchange reports
    (Binance header X-MBX-USED-WEIGHT-1M).
  * Normal requests (price data, polling) wait when the budget is above the SOFT line. Urgent requests (orders,
    stops, closes) are only held back above the HARD line.
  * On 429 / 418 / rate-limit codes: ALL pairs pause together, for as long as the exchange says (Retry-After).
    Urgent requests during a plain 429 pause wait it out; during a ban (418) one careful attempt is allowed every
    ATTEMPT_GAP seconds so a close is never stuck forever, but the ban is never hammered.
  * Spreads the candle-close crowd: normal requests start a little apart (jitter) and only a few run at once.
  * Sends ONE alert (Telegram/email through the hook) when a pause or ban starts.

Nothing here changes any trading rule. It only decides WHEN a request may be sent.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("api_guard")

ATTEMPT_GAP = 30.0          # during a ban: one urgent attempt every 30 s
MAX_WAIT_NORMAL = 120.0     # a normal request never waits longer than this for budget (then it is sent anyway
                            # only if not paused; during a pause it keeps waiting)
CONCURRENT_NORMAL = 4       # normal requests in flight at once, per exchange
JITTER_MAX = 0.35           # seconds of random spacing for normal requests
DEFAULT_429_PAUSE = 10.0
DEFAULT_418_PAUSE = 180.0


class _Window:
    """Sliding window of (time, weight)."""

    def __init__(self, span: float):
        self.span = span
        self.items: deque = deque()
        self.total = 0.0

    def _trim(self, now: float) -> None:
        while self.items and now - self.items[0][0] >= self.span:
            _, w = self.items.popleft()
            self.total -= w
        if not self.items:
            self.total = 0.0

    def used(self, now: float) -> float:
        self._trim(now)
        return self.total

    def add(self, now: float, weight: float) -> None:
        self._trim(now)
        self.items.append((now, weight))
        self.total += weight

    def wait_for(self, now: float, weight: float, limit: float) -> float:
        """Seconds until `weight` more fits under `limit` (0 = fits now)."""
        self._trim(now)
        if self.total + weight <= limit:
            return 0.0
        need = self.total + weight - limit
        freed = 0.0
        for t, w in self.items:
            freed += w
            if freed >= need:
                return max(0.0, t + self.span - now) + 0.01
        return self.span


class ApiGuard:
    def __init__(self, name: str, weight_limit: float, window: float = 60.0,
                 soft_ratio: float = 0.55, hard_ratio: float = 0.92,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep,
                 jitter: bool = True):
        self.name = name
        self.weight_limit = float(weight_limit)
        self.soft = self.weight_limit * soft_ratio
        self.hard = self.weight_limit * hard_ratio
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self.win = _Window(window)
        self.reported_used: Optional[float] = None      # exchange-reported used weight
        self.reported_at: float = 0.0
        self.pause_until: float = 0.0
        self.banned = False
        self.last_attempt: float = -1e9
        self.alert_hook: Optional[Callable[[str], None]] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._last_alert_at: float = -1e9
        self.paused_count = 0

    # ------------------------------------------------------------ state
    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(CONCURRENT_NORMAL)
        return self._sem

    def used(self) -> float:
        now = self._clock()
        local = self.win.used(now)
        if self.reported_used is not None and now - self.reported_at < self.win.span:
            return max(local, self.reported_used)
        return local

    def is_paused(self) -> bool:
        return self._clock() < self.pause_until

    def note_reported(self, used_weight) -> None:
        try:
            self.reported_used = float(used_weight)
            self.reported_at = self._clock()
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------ alerts
    def _alert(self, text: str) -> None:
        log.error("%s", text)
        now = self._clock()
        if now - self._last_alert_at < 60.0:
            return
        self._last_alert_at = now
        hook = self.alert_hook or _default_alert
        try:
            hook(text)
        except Exception as e:                          # an alert problem must never stop trading
            log.warning("api_guard alert hook failed: %s", e)

    # ------------------------------------------------------------ pause / ban
    def trigger_pause(self, seconds: float, status: Optional[int] = None, detail: str = "") -> None:
        """Called on 429 / 418 / rate-limit codes. Pauses everything on this exchange."""
        seconds = max(float(seconds), 1.0)
        ban = status == 418
        until = self._clock() + seconds
        if until > self.pause_until:
            self.pause_until = until
        if ban:
            self.banned = True
        self.paused_count += 1
        if ban:
            self._alert(f"{self.name.upper()} API BAN (418). All requests paused ~{int(seconds)}s. "
                        f"Only careful close attempts continue. {detail}".strip())
        else:
            self._alert(f"{self.name.upper()} rate limit hit. All requests paused ~{int(seconds)}s. {detail}".strip())

    def note_success(self) -> None:
        if self.banned and not self.is_paused():
            self.banned = False

    # ------------------------------------------------------------ acquire
    async def acquire(self, weight: float = 1.0, urgent: bool = False) -> None:
        """Wait until this request may be sent, then book its weight."""
        waited = 0.0
        while True:
            now = self._clock()
            if now < self.pause_until:
                if urgent and self.banned and now - self.last_attempt >= ATTEMPT_GAP:
                    self.last_attempt = now          # one careful attempt in a ban
                    break
                await self._sleep(min(self.pause_until - now, 1.0 if urgent else 2.0) + 0.01)
                continue
            limit = self.hard if urgent else self.soft
            local_wait = self.win.wait_for(now, weight, limit)
            rep_wait = 0.0
            if self.reported_used is not None and now - self.reported_at < self.win.span:
                if self.reported_used + weight > limit:
                    rep_wait = min(self.win.span - (now - self.reported_at), 5.0) + 0.01
            wait = max(local_wait, rep_wait)
            if wait <= 0:
                break
            if not urgent and waited >= MAX_WAIT_NORMAL:
                break                                 # never starve forever; the exchange will say if it is too much
            step = min(wait, 5.0)
            waited += step
            await self._sleep(step)
        if not urgent and self._jitter:
            await self._sleep(random.uniform(0.0, JITTER_MAX))
        self.win.add(self._clock(), weight)

    def normal_slot(self):
        """`async with guard.normal_slot():` - caps how many normal requests are in flight."""
        return self._semaphore()


def send_alert(text: str, kind: str = "Alert") -> None:
    """Telegram (email fallback) from whatever event loop is running. Silent if none (e.g. a worker thread)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from app import telegram_notifier as tg
    loop.create_task(tg.notifier.send(f"\u26a0\ufe0f {text}", True, email_kind=kind))


def _default_alert(text: str) -> None:
    send_alert(text, "API limit")


# ---------------------------------------------------------------- Binance weights
def binance_weight(method: str, path: str, params: Optional[dict]) -> int:
    """Request weight per Binance's USD-M Futures docs (klines depends on limit)."""
    if path.endswith("/klines"):
        try:
            limit = int((params or {}).get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        if limit < 100:
            return 1
        if limit < 500:
            return 2
        if limit <= 1000:
            return 5
        return 10
    if path.endswith("/positionRisk"):
        return 5
    if path.endswith("/exchangeInfo"):
        return 1
    if path.endswith("/account") or path.endswith("/balance"):
        return 5
    if path.endswith("/openOrders") and method == "GET" and not (params or {}).get("symbol"):
        return 40
    if path.endswith("/allOrders") or path.endswith("/userTrades"):
        return 5
    return 1


def binance_urgent(method: str, path: str) -> bool:
    """Orders, cancels, stops, listen-key keepalive are urgent; price/data polling is not."""
    if method.upper() in ("POST", "PUT", "DELETE"):
        return True
    return path.endswith("/order") or path.endswith("/openOrders") or "/algo" in path


# ---------------------------------------------------------------- shared instances
_guards: dict[str, ApiGuard] = {}


def get_guard(name: str, limit: Optional[float] = None, window: float = 60.0) -> ApiGuard:
    g = _guards.get(name)
    if g is None:
        if limit is None:
            limit = 2400 if name == "binance" else 600
        g = ApiGuard(name, weight_limit=limit, window=window)
        _guards[name] = g
    return g


# OKX: public market data is limited per IP (candles 40 per 2 s); account/trade calls per account (position and
# balance calls 10 per 2 s). Guards are kept well below those numbers.
OKX_PUBLIC_LIMIT = 40
OKX_PRIVATE_LIMIT = 12
OKX_WINDOW = 2.0


def okx_guard(signed: bool, api_key: str = "") -> ApiGuard:
    if not signed:
        return get_guard("okx-public", OKX_PUBLIC_LIMIT, OKX_WINDOW)
    return get_guard(f"okx-acct-{(api_key or 'x')[:10]}", OKX_PRIVATE_LIMIT, OKX_WINDOW)


def okx_urgent(method: str, path: str) -> bool:
    return method.upper() == "POST" or "/trade/" in path or "/algo" in path


def reset_guards_for_tests() -> None:
    _guards.clear()


def set_alert_hook(name: str, hook: Callable[[str], None]) -> None:
    get_guard(name).alert_hook = hook


def retry_after_seconds(headers, default: float) -> float:
    try:
        v = headers.get("Retry-After") if headers is not None else None
        if v is not None:
            return max(1.0, float(v))
    except (TypeError, ValueError):
        pass
    return default


def ban_seconds_from_message(data) -> float:
    """Binance 418 text: "...IP banned until <ms epoch>". Seconds from now until then (0 if not found)."""
    import re
    try:
        m = re.search(r"banned until (\d{10,})", str((data or {}).get("msg", "")) if isinstance(data, dict) else "")
        if m:
            return max(0.0, int(m.group(1)) / 1000.0 - time.time()) + 2.0
    except Exception:
        pass
    return 0.0
