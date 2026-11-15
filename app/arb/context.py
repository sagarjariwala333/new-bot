"""Shared data every module reads: prices, rules, fees, universe, clock."""
import time


class Ctx:
    def __init__(self, settings, log, risk, journal, clock=time.time):
        self.s = settings
        self.log = log
        self.risk = risk
        self.journal = journal
        self.clock = clock
        self.exchanges = []
        self.fees = {}        # (ex, market) -> {"maker", "taker"}
        self.instr = {}       # (ex, market) -> {symbol: Instr}
        self.books = {}       # (ex, market) -> {symbol: {bid, ask, bq, aq}}
        self.universe = {}    # market -> [bases]  (spatial)
        self.funding = {}     # ex -> {base: {rate, next_time_ms}}
        self.funding_universe = {}   # ex -> [bases with spot + perpetual]
        self.tri_bases = {}   # ex -> [bases] (triangular)
        self.stamp = 0        # bumped when rules/universe are refreshed
        self.volumes = {}     # (ex, market) -> {symbol: 24h USDT volume}
        self._notes = {}
        self.alert = lambda text: None      # Telegram hook (set by the trader)
        self.trade_event = lambda kind, mode, text: None   # 'trade opened / closed' message hook (set by the trader)

    def now(self):
        return self.clock()

    @staticmethod
    def symbol(ex, market, base):
        if ex == "binance":
            return base + "USDT"
        return "%s-USDT%s" % (base, "-SWAP" if market == "futures" else "")

    def book(self, ex, market, base):
        return self.books.get((ex, market), {}).get(self.symbol(ex, market, base))

    def ins(self, ex, market, base):
        return self.instr.get((ex, market), {}).get(self.symbol(ex, market, base))

    def fee(self, ex, market):
        return self.fees[(ex, market)]["taker"]

    def note(self, key, level, text, *args):
        """Log only when the message for this key changes (keeps logs readable)."""
        msg = text % args if args else text
        if self._notes.get(key) != msg:
            self._notes[key] = msg
            self.log.log(level, msg)
