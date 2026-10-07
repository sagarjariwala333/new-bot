"""
fake_client.py
==============

A minimal, deterministic stand-in for BinanceFuturesClient used only by the
test suite. Implements just the async surface BotInstance actually calls,
with configurable canned responses/failures, so tests/test_instance_safety.py
can assert exact behavior without touching a real network or a real Binance
account.

Models TWO SEPARATE Binance order systems, matching reality since the
2025-12-09 conditional-order migration:
  - self.open_orders: regular orders (MARKET/LIMIT) - orderId/clientOrderId/
    type/stopPrice/status. This bot's entries/closes are MARKET orders that
    fill instantly, so this list is rarely populated in practice.
  - self.open_algo_orders: algo (conditional) orders - algoId/clientAlgoId/
    orderType/triggerPrice/algoStatus. This is where this bot's SL/TP/
    trailing-stop orders actually live.
"""

from __future__ import annotations

import random

from app.binance_futures import BinanceAPIError, SymbolInfo, CLIENT_ORDER_ID_PREFIX, OWN_ORDER_PREFIXES


class FakeClient:
    # Step 3 (2026-09-14): instance.py now reads the "is this our order"
    # prefix from self.client.client_order_id_prefix (a real adapter
    # property on both BinanceFuturesClient and OKXFuturesClient), rather
    # than importing a hardcoded constant - FakeClient needs the same
    # attribute so tests that exercise reconciliation/cleanup code paths
    # keep working unchanged.
    client_order_id_prefix = OWN_ORDER_PREFIXES

    def __init__(self):
        self.open_orders: list[dict] = []        # regular orders (rarely used by this bot)
        self.open_algo_orders: list[dict] = []    # algo (conditional) orders - SL/TP/trailing live here
        self.position: dict | None = None
        self.all_positions: list[dict] | None = None  # account-wide positions for the exposure cap test
        self.mark_price = 100.0
        self.equity = 10_000.0
        self.available_balance = 10_000.0  # defaults to match .equity; set independently to test the distinction

        self.cancel_order_calls: list[int] = []       # algoId values passed to cancel_algo_order
        self.market_orders: list[tuple[str, str, float]] = []
        self.close_orders: list[tuple[str, str, float]] = []
        self.stop_orders: list[tuple[str, str, float]] = []
        self.tp_orders: list[tuple[str, str, float]] = []

        self._next_id = 1000
        self.fail_stop_market_order = False
        self.fail_take_profit_order = False
        self.fail_close_position = False
        self.fail_cancel_all_open_orders = False
        self.close_result_confirms_flat = True  # whether position_risk returns None after a close

        # 2026-10-01 per-trade leverage: every set_leverage call, and the order of set_leverage vs
        # market_order calls, so tests can prove leverage is set BEFORE the entry order.
        self.leverage_calls: list[int] = []
        self.call_order: list[str] = []
        self.fail_set_leverage = False

        # regular order_id -> list of fill dicts with real "commission" -
        # configurable per test so _fetch_actual_commission can be tested
        # deterministically.
        self.user_trades_by_order_id: dict[int, list[dict]] = {}
        self.get_user_trades_calls: list[int] = []
        self.fail_get_user_trades = False

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # ---------------------------------------------------------------- reads
    async def get_open_orders(self, symbol: str) -> list[dict]:
        return list(self.open_orders)

    async def get_position_risk(self, symbol: str) -> dict | None:
        return self.position

    async def get_all_open_positions(self) -> list[dict]:
        """Test double: returns whatever the test configures via
        self.all_positions, defaulting to reflecting the single self.position
        if set (so existing tests that only set .position still see
        consistent exposure-check behavior without needing to also set this
        separately)."""
        if self.all_positions is not None:
            return self.all_positions
        if self.position is not None:
            return [self.position]
        return []

    async def get_mark_price(self, symbol: str) -> float:
        return self.mark_price

    async def get_equity(self) -> float:
        return self.equity

    async def get_available_balance(self) -> float:
        return self.available_balance

    # ---------------------------------------------------------------- algo (conditional) orders
    async def get_open_algo_orders(self, symbol: str) -> list[dict]:
        return list(self.open_algo_orders)

    async def get_algo_order(self, algo_id: int) -> dict:
        for o in self.open_algo_orders:
            if o["algoId"] == algo_id:
                return {**o, "algoStatus": "NEW", "actualOrderId": "", "actualPrice": "0.00000"}
        # Not found among currently-resting orders - simulate "it triggered".
        return {
            "algoId": algo_id, "algoStatus": "TRIGGERED",
            "actualOrderId": str(self._new_id()), "actualPrice": str(self.mark_price),
            "triggerPrice": str(self.mark_price),
        }

    async def get_user_trades(self, symbol: str, order_id: int | None = None, limit: int = 50) -> list[dict]:
        self.get_user_trades_calls.append(order_id)
        if self.fail_get_user_trades:
            raise BinanceAPIError(500, {"code": -1001, "msg": "simulated failure"})
        if order_id is None:
            return [f for fills in self.user_trades_by_order_id.values() for f in fills]
        return self.user_trades_by_order_id.get(order_id, [])

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        # market_qty_step/market_min_qty mirror qty_step/min_qty by default,
        # matching the real client's own fallback for a symbol without a
        # separate MARKET_LOT_SIZE filter - tests that need to exercise a
        # genuinely different market-specific limit can override these
        # directly on the returned object or via a subclass.
        return SymbolInfo(symbol=symbol, price_tick=0.01, qty_step=0.001,
                           min_qty=0.001, min_notional=5.0,
                           market_qty_step=0.001, market_min_qty=0.001,
                           market_max_qty=float("inf"), max_notional=float("inf"))

    async def get_leverage_brackets(self, symbol: str) -> list[dict]:
        # Generous default (up to 125x with no practical notional cap) so
        # existing tests are unaffected unless they explicitly override
        # this to exercise the leverage-bracket check itself.
        return [{"max_leverage": 125, "notional_floor": 0.0, "notional_cap": float("inf")}]

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        """Enough synthetic OHLCV history (with a little noise, not a dead-flat
        line) to satisfy any min_bars_required check and produce a real,
        finite, positive ATR - used by tests that exercise the zero-ATR
        recovery fallback (_fetch_snapshot inside _resolve_unprotected_position)."""
        rng = random.Random(7)
        n = max(limit, 300)
        candles = []
        price = self.mark_price
        for i in range(n):
            o = price
            c = price + rng.uniform(-0.3, 0.3)
            h = max(o, c) + 0.4
            l = min(o, c) - 0.4
            price = c
            candles.append([i, str(o), str(h), str(l), str(c), "1", i, "1", 1, "1", "1", "0"])
        return candles

    # ---------------------------------------------------------------- setup
    async def set_leverage(self, symbol: str, leverage: int):
        if self.fail_set_leverage:
            raise BinanceAPIError(400, {"code": -4028, "msg": "simulated set_leverage failure"})
        self.leverage_calls.append(int(leverage))
        self.call_order.append(f"set_leverage:{int(leverage)}")
        return {}

    async def set_margin_type(self, symbol: str, isolated: bool):
        return {}

    async def verify_one_way_mode(self):
        return None  # fake client always reports one-way mode confirmed

    # ---------------------------------------------------------------- rounding/validation
    async def round_qty(self, symbol: str, qty: float) -> float:
        return round(qty, 6)

    async def round_price(self, symbol: str, price: float) -> float:
        return round(price, 2)

    async def check_min_notional(self, symbol: str, qty: float, price: float):
        return True, ""

    @staticmethod
    def validate_stop_side(direction: str, stop_price: float, mark_price: float):
        if direction == "LONG" and stop_price >= mark_price:
            return False, "would trigger immediately"
        if direction == "SHORT" and stop_price <= mark_price:
            return False, "would trigger immediately"
        return True, ""

    # ---------------------------------------------------------------- orders
    async def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        self.market_orders.append((symbol, side, quantity))
        self.call_order.append("market_order")
        return {"orderId": self._new_id(), "avgPrice": str(self.mark_price)}

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        self.close_orders.append((symbol, side, quantity))
        if self.fail_close_position:
            raise BinanceAPIError(400, {"code": -2022, "msg": "ReduceOnly Order is rejected"})
        if self.close_result_confirms_flat:
            self.position = None
        return {"orderId": self._new_id(), "avgPrice": str(self.mark_price)}

    async def stop_market_order(self, symbol: str, side: str, trigger_price: float,
                                 skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        if self.fail_stop_market_order:
            raise BinanceAPIError(400, {"code": -1013, "msg": "simulated failure"})
        algo_id = self._new_id()
        self.stop_orders.append((symbol, side, trigger_price))
        self.open_algo_orders.append({
            "algoId": algo_id, "orderType": "STOP_MARKET", "triggerPrice": str(trigger_price),
            "clientAlgoId": "hullbot_fake_sl", "algoStatus": "NEW",
        })
        return {"algoId": algo_id}

    async def take_profit_market_order(self, symbol: str, side: str, trigger_price: float,
                                        skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        if self.fail_take_profit_order:
            raise BinanceAPIError(400, {"code": -1013, "msg": "simulated failure"})
        algo_id = self._new_id()
        self.tp_orders.append((symbol, side, trigger_price))
        self.open_algo_orders.append({
            "algoId": algo_id, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": str(trigger_price),
            "clientAlgoId": "hullbot_fake_tp", "algoStatus": "NEW",
        })
        return {"algoId": algo_id}

    async def cancel_algo_order(self, algo_id: int):
        self.cancel_order_calls.append(algo_id)
        self.open_algo_orders = [o for o in self.open_algo_orders if o["algoId"] != algo_id]
        return {"algoId": algo_id, "code": "200", "msg": "success"}

    async def cancel_regular_order(self, symbol: str, order_id: int):
        self.open_orders = [o for o in self.open_orders if o.get("orderId") != order_id]
        return {"orderId": order_id, "status": "CANCELED"}

    async def cancel_all_algo_open_orders(self, symbol: str):
        self.open_algo_orders = []
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    async def cancel_all_open_orders(self, symbol: str):
        # Deliberately NOT called by any automatic code path except the
        # explicit, owner-approved pre-entry cleanup (see F-01/0g) - tests
        # assert this. Clears BOTH order systems, matching the real client.
        if self.fail_cancel_all_open_orders:
            raise BinanceAPIError(500, {"code": -1001, "msg": "simulated cleanup failure"})
        self.open_orders = []
        self.open_algo_orders = []
        return {}

    async def close(self):
        return None

    # ---------------------------------------------------------------- user data stream (listenKey)
    async def create_listen_key(self) -> str:
        return "fake_listen_key"

    async def keepalive_listen_key(self, listen_key: str):
        return {}

    async def close_listen_key(self, listen_key: str):
        return {}
