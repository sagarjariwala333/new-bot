"""Trading rules for one instrument on one exchange."""
from app.arb.dec import D, ceil_to, floor_to


class Instr:
    """Quantities are always in BASE coins here. `unit` converts to the
    exchange's own unit (OKX swaps count contracts; everything else is 1)."""

    __slots__ = ("ex", "market", "symbol", "base", "quote", "qty_step", "min_qty",
                 "tick", "min_notional", "unit")

    def __init__(self, ex, market, symbol, base, quote, qty_step, min_qty, tick,
                 min_notional=0, unit=1):
        self.ex, self.market, self.symbol = ex, market, symbol
        self.base, self.quote = base, quote
        self.qty_step, self.min_qty = D(qty_step), D(min_qty)
        self.tick, self.min_notional = D(tick), D(min_notional)
        self.unit = D(unit)

    def native_qty(self, base_qty):
        return D(base_qty) / self.unit

    def from_native(self, native):
        return D(native) * self.unit

    def round_qty(self, qty):
        return floor_to(qty, self.qty_step)

    def round_price(self, price, up):
        return ceil_to(price, self.tick) if up else floor_to(price, self.tick)

    def size_ok(self, qty, price):
        qty = D(qty)
        return qty >= self.min_qty and qty * D(price) >= self.min_notional and qty > 0

    def to_dict(self):
        return {k: str(getattr(self, k)) if k in ("qty_step", "min_qty", "tick", "min_notional", "unit")
                else getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(d["ex"], d["market"], d["symbol"], d["base"], d["quote"], d["qty_step"],
                   d["min_qty"], d["tick"], d["min_notional"], d["unit"])

    def __repr__(self):
        return "Instr(%s %s %s step=%s)" % (self.ex, self.market, self.symbol, self.qty_step)
