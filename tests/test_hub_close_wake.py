"""WAKE AT THE CANDLE CLOSE (owner 2026-10-24: "It should flip instantaneous.. else we catch running trend").
The bot wakes right at the candle close, not at its next poll. Arbitrary test numbers only."""
import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase  # noqa: E402  (stubs + temp data dir)
from app import hub_trading as HT  # noqa: E402

H1 = 3_600_000


class _Obj(HT.HubMixin):
    def __init__(self, tf, last):
        class PC:
            timeframe = tf
        class HS:
            last_processed_candle = last
        self.pc, self.hub = PC(), HS()


class TestWakeTime(unittest.TestCase):
    def test_wakes_half_a_second_after_the_close(self):
        o = _Obj("1h", 10 * H1)                         # candle 10h processed; candle 11h closes at 12h
        self.assertAlmostEqual(o._hub_close_wake_in(now_s=(12 * H1 - 7_000) / 1000), 7.5)

    def test_close_passed_not_seen_yet_looks_again_every_second(self):
        o = _Obj("1h", 10 * H1)
        self.assertEqual(o._hub_close_wake_in(now_s=(12 * H1 + 2_000) / 1000), 1.0)
        self.assertEqual(o._hub_close_wake_in(now_s=(12 * H1 + 30_000) / 1000), 1.0)
        self.assertIsNone(o._hub_close_wake_in(now_s=(12 * H1 + 31_000) / 1000))   # then the poll timing

    def test_other_timeframes(self):
        o = _Obj("4h", 0)
        self.assertAlmostEqual(o._hub_close_wake_in(now_s=(8 * H1 - 1_000) / 1000), 1.5)
        o = _Obj("1m", 0)
        self.assertAlmostEqual(o._hub_close_wake_in(now_s=119.0), 1.5)

    def test_poll_timing_kept_when_unknown(self):
        self.assertIsNone(_Obj("1M", 0)._hub_close_wake_in(now_s=1.0))             # months have no fixed length
        self.assertIsNone(_Obj("1h", None)._hub_close_wake_in(now_s=1.0))          # nothing processed yet


class TestSleepEndsAtTheClose(LadderBase):
    async def test_sleep_stops_at_the_close_not_at_the_poll(self):
        inst = self.make_l()
        inst.pc.poll_seconds = 60
        inst.hub.last_processed_candle = 10 * H1
        clock = {"mono": 0.0, "wall": (12 * H1 - 3_000) / 1000}      # 3 s before the close

        class _FakeTime:
            def monotonic(self):
                return clock["mono"]

            def time(self):
                return clock["wall"]

            def __getattr__(self, name):
                return getattr(time, name)

        async def fake_sleep(sec):
            clock["mono"] += sec
            clock["wall"] += sec
        with mock.patch("app.instance.time", _FakeTime()), mock.patch("app.hub_trading.time", _FakeTime()), \
                mock.patch("app.instance.asyncio.sleep", new=fake_sleep):
            await inst._sleep_until_next_tick()
        self.assertAlmostEqual(clock["mono"], 3.5)                     # woke 0.5 s after the close, not after 60 s

    async def test_far_from_the_close_the_poll_is_kept(self):
        inst = self.make_l()
        inst.pc.poll_seconds = 20
        inst.hub.last_processed_candle = 10 * H1
        clock = {"mono": 0.0, "wall": (11 * H1 + 60_000) / 1000}     # close is ~59 min away

        class _FakeTime:
            def monotonic(self):
                return clock["mono"]

            def time(self):
                return clock["wall"]

            def __getattr__(self, name):
                return getattr(time, name)

        async def fake_sleep(sec):
            clock["mono"] += sec
            clock["wall"] += sec
        with mock.patch("app.instance.time", _FakeTime()), mock.patch("app.hub_trading.time", _FakeTime()), \
                mock.patch("app.instance.asyncio.sleep", new=fake_sleep):
            await inst._sleep_until_next_tick()
        self.assertAlmostEqual(clock["mono"], 20.0)


if __name__ == "__main__":
    unittest.main()
