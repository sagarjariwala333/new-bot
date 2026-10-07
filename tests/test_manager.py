import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

TEST_DATA_DIR = Path("/tmp/hull_bot_test_manager")

import app.store as store_module  # noqa: E402
import app.manager as manager_module  # noqa: E402
from app.manager import BotManager, _key  # noqa: E402
from app import singleton_lock  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class TestManualStartRefusedDuringSingletonConflict(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix, flagged by a third-party review (confirmed real and
    serious): manual start/restart previously never checked the singleton
    lock at all - a conflicting second process could correctly skip its
    own automatic startup, yet still trade the moment anyone hit a manual
    start endpoint on it."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()
        manager_module.store = self.store
        self.manager = BotManager()
        self.manager.store = self.store

        from app.store import PairConfig
        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

    def tearDown(self):
        singleton_lock._lock_held = True  # see test_singleton_lock.py's own tearDown for why

    async def test_manual_start_refused_while_lock_conflict_is_active(self):
        singleton_lock._lock_held = False
        with self.assertRaises(RuntimeError):
            await self.manager.start(self.acc.id, "BTCUSDT")
        self.assertNotIn(_key(self.acc.id, "BTCUSDT"), self.manager.instances,
                         "must not actually start the pair when refused")

    async def test_manual_start_succeeds_once_the_lock_is_genuinely_held(self):
        singleton_lock._lock_held = True
        instance = await self.manager.start(self.acc.id, "BTCUSDT")
        self.assertIsNotNone(instance)
        self.assertIn(_key(self.acc.id, "BTCUSDT"), self.manager.instances)


class FakeDoneTask:
    """Stands in for an asyncio.Task that has already finished."""
    def done(self):
        return True


class FakeRunningTask:
    def done(self):
        return False


class TestStartAllEnabledIsolation(unittest.IsolatedAsyncioTestCase):
    """Team M's finding: one pair failing to even build (corrupted
    credentials, bad encryption key, malformed config) must not stop
    start_all_enabled() from starting every other enabled pair, and must
    not propagate up through main.py's lifespan handler and prevent the
    entire web server from starting at all."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()
        manager_module.store = self.store

        self.acc = self.store.create_account("Main", "key", "secret", True)
        from app.store import PairConfig
        for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            pc = tv.pair_config(symbol=sym)
            self.store.add_pair(self.acc.id, pc)
            self.store.set_pair_enabled(self.acc.id, sym, True)

        self.manager = BotManager()
        self.manager.store = self.store

        from app.instance import BotInstance
        self._original_instance_start = BotInstance.start
        BotInstance.start = lambda self: None  # don't spawn real background tasks in this test

    def tearDown(self):
        from app.instance import BotInstance
        BotInstance.start = self._original_instance_start

    async def test_one_broken_pair_does_not_stop_the_others_from_starting(self):
        original_build = self.manager._build_instance

        def failing_build(account_id, symbol):
            if symbol == "BBBUSDT":
                raise ValueError("simulated corrupted credentials")
            return original_build(account_id, symbol)

        self.manager._build_instance = failing_build

        await self.manager.start_all_enabled()  # must not raise, despite BBBUSDT failing

        self.assertIn(_key(self.acc.id, "AAAUSDT"), self.manager.instances,
                     "a healthy pair before the broken one must still start")
        self.assertIn(_key(self.acc.id, "CCCUSDT"), self.manager.instances,
                     "a healthy pair after the broken one must still start")
        self.assertNotIn(_key(self.acc.id, "BBBUSDT"), self.manager.instances,
                         "the broken pair itself must not have a (half-built) instance registered")

    async def test_broken_pair_is_recorded_in_startup_failures(self):
        original_build = self.manager._build_instance

        def failing_build(account_id, symbol):
            if symbol == "BBBUSDT":
                raise ValueError("simulated corrupted credentials")
            return original_build(account_id, symbol)

        self.manager._build_instance = failing_build

        await self.manager.start_all_enabled()

        key = _key(self.acc.id, "BBBUSDT")
        self.assertIn(key, self.manager.startup_failures)
        self.assertIn("simulated corrupted credentials", self.manager.startup_failures[key])
        # Healthy pairs must NOT show up as failures.
        self.assertNotIn(_key(self.acc.id, "AAAUSDT"), self.manager.startup_failures)

    async def test_successful_start_clears_a_previously_recorded_failure(self):
        key = _key(self.acc.id, "AAAUSDT")
        self.manager.startup_failures[key] = "stale failure from a previous attempt"

        await self.manager.start_all_enabled()

        self.assertNotIn(key, self.manager.startup_failures,
                         "a pair that starts successfully must have any stale failure marker cleared")


class TestManagerRestartAfterError(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()
        self.acc = self.store.create_account("Main", "key", "secret", True)
        from app.store import PairConfig
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

        self.manager = BotManager()
        self.manager.store = self.store
        # _build_instance() looks up the module-level `store` name (imported
        # at manager.py's top), not self.store - patch that reference too so
        # this test's account is actually visible to it.
        manager_module.store = self.store

    def test_dead_error_instance_is_rebuilt_not_reused(self):
        """Regression test: previously, an instance stuck in ERROR (its task
        already exited, e.g. because startup reconciliation failed) was
        treated as 'still running' and silently returned as-is - clicking
        Start again did nothing. Liveness must be judged by the actual task,
        not the status string."""
        key = _key(self.acc.id, "BTCUSDT")

        class DummyInstance:
            def __init__(self):
                self.state = type("S", (), {"status": "ERROR"})()
                self._task = FakeDoneTask()

        dead = DummyInstance()
        self.manager.instances[key] = dead

        import asyncio
        from app.instance import BotInstance

        # Only checking identity/rebuild behavior here, not the real bot
        # loop - stub out the actual task spawn so this test doesn't leave a
        # background task hitting a fake network client.
        original_start = BotInstance.start
        BotInstance.start = lambda self: None
        try:
            async def run():
                return await self.manager.start(self.acc.id, "BTCUSDT")

            result = asyncio.run(run())
        finally:
            BotInstance.start = original_start

        self.assertIsNot(result, dead, "a dead (task-done) ERROR instance must be rebuilt, not reused")

    def test_still_running_instance_is_left_alone(self):
        key = _key(self.acc.id, "BTCUSDT")

        class DummyInstance:
            def __init__(self):
                self.state = type("S", (), {"status": "IN_POSITION"})()
                self._task = FakeRunningTask()

        alive = DummyInstance()
        self.manager.instances[key] = alive

        import asyncio

        async def run():
            return await self.manager.start(self.acc.id, "BTCUSDT")

        result = asyncio.run(run())
        self.assertIs(result, alive, "an instance whose task is still running must not be replaced")


class TestUserDataStreamLifecycle(unittest.IsolatedAsyncioTestCase):
    """Two independent third-party reviews caught the same real bug: the
    shared per-account user-data stream stored whichever PAIR's client
    happened to create it, and BotInstance.stop() closes its own client -
    so if that specific pair stopped while sibling pairs kept running, the
    stream was left holding a dead client for its own keepalive/reconnect
    calls. Fixed by giving the stream its own dedicated client, built
    directly from the account's credentials, never borrowed from any pair.
    Also fixed: teardown is now properly awaited, not fire-and-forget."""

    def setUp(self):
        from app.user_data_stream import UserDataStream
        self.manager = BotManager()
        self._original_stream_start = UserDataStream.start
        # Don't actually spawn a real background task / network connection -
        # same pattern already used for BotInstance.start in this file.
        UserDataStream.start = lambda self: None

    def tearDown(self):
        from app.user_data_stream import UserDataStream
        UserDataStream.start = self._original_stream_start

    def test_stream_gets_its_own_dedicated_client_not_a_pairs_own(self):
        pair_a_client_stand_in = object()  # represents "pair A's own client" - must never be reused

        stream = self.manager.get_or_create_user_data_stream("acc1", "key123", "secret456", True)

        self.assertIsNot(stream.client, pair_a_client_stand_in)
        self.assertTrue(hasattr(stream.client, "create_listen_key"),
                        "must be a real, independent BinanceFuturesClient, not a placeholder")

    def test_same_account_reuses_the_same_stream_and_client(self):
        stream1 = self.manager.get_or_create_user_data_stream("acc1", "key123", "secret456", True)
        stream2 = self.manager.get_or_create_user_data_stream("acc1", "key123", "secret456", True)

        self.assertIs(stream1, stream2, "a second pair on the same account must reuse the same stream")
        self.assertIs(stream1.client, stream2.client, "and therefore the same dedicated client")

    async def test_stopping_one_pair_does_not_affect_the_stream_for_siblings(self):
        stream = self.manager.get_or_create_user_data_stream("acc1", "key123", "secret456", True)
        stream.register("AAAUSDT")
        stream.register("BBBUSDT")

        # Pair A stops - unregisters itself, but B is still registered, so
        # the stream must NOT be torn down and its client must NOT be closed.
        stream.unregister("AAAUSDT")
        await self.manager.maybe_teardown_user_data_stream("acc1")

        self.assertIn("acc1", self.manager.user_data_streams, "the stream must survive while B is still registered")
        self.assertTrue(stream.has_subscribers())

    async def test_last_pair_stopping_tears_down_and_awaits_client_close(self):
        stream = self.manager.get_or_create_user_data_stream("acc1", "key123", "secret456", True)
        stream.register("AAAUSDT")

        closed = {"called": False}
        original_close = stream.client.close

        async def tracking_close():
            closed["called"] = True
            await original_close()

        stream.client.close = tracking_close

        stream.unregister("AAAUSDT")
        await self.manager.maybe_teardown_user_data_stream("acc1")

        self.assertNotIn("acc1", self.manager.user_data_streams, "the stream must be removed once unused")
        self.assertTrue(closed["called"],
                        "the stream's own dedicated client must be closed - and awaited, not fire-and-forget")


if __name__ == "__main__":
    unittest.main()
