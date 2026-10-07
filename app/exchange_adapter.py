"""
exchange_adapter.py
====================

BaseExchangeAdapter is the contract between the trading engine (instance.py)
and whichever exchange is actually executing orders. It exists so instance.py
never has to know or care whether it's talking to Binance, OKX, or anything
else added later - it only calls these methods.

STEP 1 SCOPE (2026-09-14): this file introduces the interface and makes the
existing BinanceFuturesClient formally satisfy it. Nothing about instance.py's
logic, call sites, or behavior changes in this step - it still holds a
`self.client` reference and calls the exact same methods it always has. The
only difference is that `self.client` is now provably an implementation of
this interface, which is what makes it possible to substitute an OKXAdapter
later without touching instance.py's trading logic at all.

Every method signature below was extracted directly from the methods
instance.py currently calls on BinanceFuturesClient (see the grep audit in
this session) - this is not a fresh design, it's a formalization of the
contract that already exists implicitly.

validate_stop_side is a `@staticmethod` on the concrete implementation (it
takes no exchange state - it's pure price-comparison math) so it is declared
here the same way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING


class ExchangeAPIError(Exception):
    """Common base for BinanceAPIError and OKXAPIError. instance.py catches
    THIS (not either exchange-specific subclass) in every place it needs to
    react to 'the exchange rejected/failed this call' generically, so the
    exact same except-blocks work correctly no matter which adapter a given
    BotInstance is holding. Exchange-specific code (the two adapters
    themselves) can still catch their own concrete subclass when they need
    to inspect exchange-specific fields like .code."""

if TYPE_CHECKING:
    # Import only for type checkers, not at runtime - binance_futures.py
    # inherits from BaseExchangeAdapter, so a real top-level import here
    # would be circular. `from __future__ import annotations` (above) means
    # the -> SymbolInfo return annotation is never evaluated at runtime
    # anyway, so this is safe.
    from app.binance_futures import SymbolInfo


class BaseExchangeAdapter(ABC):
    """Abstract interface every exchange adapter (Binance, OKX, ...) must
    implement. instance.py is written entirely against this interface."""

    @property
    @abstractmethod
    def client_order_id_prefix(self) -> tuple[str, ...]:
        """The tag(s) that mark an order as this bot's own - the current tag
        first (Binance: 'hullbot_', OKX: 'hullbotokx'), then any extra
        recognised tags (none in this package). Different charset
        rules per exchange). instance.py uses THIS, not a hardcoded
        constant, to recognize its own orders during cleanup/reconciliation
        - so the same filtering logic works correctly no matter which
        adapter is in use."""

    @property
    @abstractmethod
    def supports_realtime_stream(self) -> bool:
        """Whether this adapter has a real-time (websocket) order/position
        update stream wired up (see manager.py's
        get_or_create_user_data_stream, Binance-only as of this writing).
        False for an adapter without one - BotInstance already treats the
        real-time stream as a pure latency optimization on top of REST
        polling (which remains the safety net regardless), so this simply
        opts out of attempting it rather than needing any special-casing
        in instance.py's trading loop."""

    # ---------------------------------------------------------------- lifecycle
    @abstractmethod
    async def close(self):
        """Release any held connections/sessions for this adapter."""

    @abstractmethod
    async def verify_one_way_mode(self):
        """Confirm the account is in one-way (net) position mode, matching
        this strategy's assumption of a single position per symbol. Should
        raise if the account is in a mode this bot doesn't support."""

    # ---------------------------------------------------------------- market data
    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        """Return recent candles for symbol/interval, most-recent-last."""

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float:
        """Current mark price for symbol."""

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Tick size / step size / min-notional / market-order limits for symbol."""

    # ---------------------------------------------------------------- account state
    @abstractmethod
    async def get_equity(self) -> float:
        """Total account equity (used for Max Loss Cap baseline)."""

    @abstractmethod
    async def get_available_balance(self) -> float:
        """Available (non-margined) balance (used for entry position sizing)."""

    @abstractmethod
    async def get_position_risk(self, symbol: str) -> dict | None:
        """Current open position for symbol, or None if flat."""

    @abstractmethod
    async def get_all_open_positions(self) -> list[dict]:
        """Every open position on this account, across symbols."""

    # ---------------------------------------------------------------- leverage/margin
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int):
        ...

    @abstractmethod
    async def set_margin_type(self, symbol: str, isolated: bool):
        ...

    # ---------------------------------------------------------------- sizing/rounding
    @abstractmethod
    async def round_qty(self, symbol: str, qty: float) -> float:
        ...

    @abstractmethod
    async def round_price(self, symbol: str, price: float) -> float:
        ...

    @abstractmethod
    async def check_min_notional(self, symbol: str, qty: float, price: float) -> tuple[bool, str]:
        ...

    @abstractmethod
    async def get_leverage_brackets(self, symbol: str) -> list[dict]:
        """Returns this symbol's leverage-tier table, sorted ascending by
        bracket, each entry shaped as
        {"max_leverage": int, "notional_floor": float, "notional_cap": float}.
        Added 2026-09-15 - the pre-existing min/max notional filter check
        catches an order the exchange will flatly reject, but says nothing
        about whether the CONFIGURED LEVERAGE is actually achievable at
        this position's notional value - a large enough position at high
        leverage can land in a lower-leverage bracket than requested,
        which the exchange handles by capping/rejecting depending on
        venue, not something this bot should discover only after the
        fact."""

    @staticmethod
    @abstractmethod
    def validate_stop_side(direction: str, stop_price: float, mark_price: float) -> tuple[bool, str]:
        """Reject a stop price that would trigger instantly given the current
        mark price (wrong side of the market)."""

    # ---------------------------------------------------------------- entries/exits
    @abstractmethod
    async def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        ...

    @abstractmethod
    async def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        ...

    # ---------------------------------------------------------------- native stop/TP (algo) orders
    # trigger_px_type (2026-09-16, owner request - wiring an OKX dashboard
    # setting that was previously accepted but silently ignored): which
    # price basis the exchange evaluates the trigger against. Binance's
    # implementation ignores this and always uses MARK_PRICE regardless
    # (see binance_futures.py's own docstring on why) - the parameter
    # exists on this shared interface only so OKX's real, per-pair choice
    # can flow through from instance.py without a platform-specific branch
    # at the call site.
    @abstractmethod
    async def stop_market_order(self, symbol: str, side: str, trigger_price: float,
                                 skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        ...

    @abstractmethod
    async def take_profit_market_order(self, symbol: str, side: str, trigger_price: float,
                                        skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        ...

    @abstractmethod
    async def get_open_algo_orders(self, symbol: str) -> list[dict]:
        ...

    @abstractmethod
    async def get_algo_order(self, algo_id) -> dict:
        ...

    @abstractmethod
    async def cancel_algo_order(self, algo_id):
        ...

    # ---------------------------------------------------------------- regular orders / cleanup
    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[dict]:
        ...

    @abstractmethod
    async def cancel_all_open_orders(self, symbol: str):
        ...

    # ---------------------------------------------------------------- trade history
    @abstractmethod
    async def get_user_trades(self, symbol: str, order_id=None, limit: int = 50) -> list[dict]:
        ...
