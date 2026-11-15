"""Candle cache gives the same list as a full download (2026-11-11)."""
import asyncio
import unittest

from app.candle_cache import cached_klines


def row(i, close=None):
    t = i * 14400000
    c = str(100 + i) if close is None else str(close)
    return [t, "1", "2", "0.5", c, "10", t + 1, "0", 0, "0", "0", "0"]


class Fake:
    def __init__(self, n):
        self.n = n               # candles exist 0..n-1, the last one is forming
        self.calls = []
        self.override = {}

    async def get_klines(self, symbol, interval, limit=500):
        self.calls.append(limit)
        rows = [row(i, self.override.get(i)) for i in range(max(0, self.n - limit), self.n)]
        return rows


def run(c):
    return asyncio.run(c)


class CacheTests(unittest.TestCase):
    def test_same_as_full_and_small_calls(self):
        f = Fake(1100)
        first = run(cached_klines(f, "ETHUSDT", "4h", 1001))
        self.assertEqual(f.calls, [1001])
        f.n += 1                                  # a candle closed
        got = run(cached_klines(f, "ETHUSDT", "4h", 1001))
        self.assertEqual(f.calls[-1], 8)
        self.assertEqual(got, [row(i) for i in range(f.n - 1001, f.n)])
        self.assertEqual(len(got), 1001)
        got2 = run(cached_klines(f, "ETHUSDT", "4h", 1001))     # same candle again
        self.assertEqual(got2, got)
        self.assertEqual(len(first), 1001)

    def test_forming_candle_changes(self):
        f = Fake(1100)
        run(cached_klines(f, "X", "4h", 1001))
        f.override[1099] = 555
        got = run(cached_klines(f, "X", "4h", 1001))
        self.assertEqual(f.calls[-1], 8)
        self.assertEqual(got[-1][4], "555")

    def test_changed_old_candle_forces_full(self):
        f = Fake(1100)
        run(cached_klines(f, "X", "4h", 1001))
        f.override[1096] = 999                    # a closed candle differs
        got = run(cached_klines(f, "X", "4h", 1001))
        self.assertEqual(f.calls[-1], 1001)
        self.assertEqual(got[-4][4], "999")

    def test_gap_forces_full(self):
        f = Fake(1100)
        run(cached_klines(f, "X", "4h", 1001))
        f.n += 20                                 # bot was away for 20 candles
        got = run(cached_klines(f, "X", "4h", 1001))
        self.assertEqual(f.calls[-1], 1001)
        self.assertEqual(got, [row(i) for i in range(f.n - 1001, f.n)])

    def test_error_propagates(self):
        f = Fake(1100)
        run(cached_klines(f, "X", "4h", 1001))
        async def bad(*a, **k):
            raise RuntimeError("net")
        f.get_klines = bad
        with self.assertRaises(RuntimeError):
            run(cached_klines(f, "X", "4h", 1001))


if __name__ == "__main__":
    unittest.main()
