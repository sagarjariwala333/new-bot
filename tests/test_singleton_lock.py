import os
import sys
import shutil
import time
import json
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_singleton_lock")

import app.singleton_lock as singleton_lock  # noqa: E402


class TestSingletonLock(unittest.IsolatedAsyncioTestCase):
    """2026-09-16, owner request: prevent two copies of this bot from
    trading at once. Deliberately a heartbeat, not a PID file - a PID
    lock is meaningless across two separate Railway containers, but both
    share the same persistent volume, so a shared timestamp file works."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        singleton_lock.HEARTBEAT_FILE = TEST_DATA_DIR / "heartbeat.json"

    def tearDown(self):
        # These tests deliberately flip singleton_lock._lock_held (a
        # module-level flag other tests rely on defaulting to True, since
        # they never call check_and_claim_lock() at all - the exact class
        # of test-pollution bug already hit once tonight with the shared
        # rate-limit tracker). Restoring it here means this file's own
        # test order, or any other file's, can never leak a False value
        # into an unrelated test - not relying on alphabetical discovery
        # order to happen to save us.
        singleton_lock._lock_held = True

    def test_no_existing_heartbeat_claims_the_lock(self):
        claimed, message = singleton_lock.check_and_claim_lock()
        self.assertTrue(claimed)
        self.assertIsNone(message)
        self.assertTrue(singleton_lock.HEARTBEAT_FILE.exists(),
                        "claiming the lock must write a fresh heartbeat immediately")

    def test_fresh_heartbeat_from_another_instance_blocks_the_lock(self):
        singleton_lock.HEARTBEAT_FILE.write_text(json.dumps({
            "instance_id": "other-instance", "hostname": "other-host", "updated_at": time.time(),
        }))
        claimed, message = singleton_lock.check_and_claim_lock()
        self.assertFalse(claimed)
        self.assertIn("other-instance", message)
        self.assertIn("other-host", message)

    def test_stale_heartbeat_does_not_block_the_lock(self):
        stale_time = time.time() - (singleton_lock.STALE_THRESHOLD_SECONDS + 10)
        singleton_lock.HEARTBEAT_FILE.write_text(json.dumps({
            "instance_id": "old-instance", "hostname": "old-host", "updated_at": stale_time,
        }))
        claimed, message = singleton_lock.check_and_claim_lock()
        self.assertTrue(claimed)
        self.assertIsNone(message)

    def test_claiming_overwrites_the_stale_heartbeat_with_a_fresh_one(self):
        stale_time = time.time() - (singleton_lock.STALE_THRESHOLD_SECONDS + 10)
        singleton_lock.HEARTBEAT_FILE.write_text(json.dumps({
            "instance_id": "old-instance", "hostname": "old-host", "updated_at": stale_time,
        }))
        singleton_lock.check_and_claim_lock()
        new_data = json.loads(singleton_lock.HEARTBEAT_FILE.read_text())
        self.assertEqual(new_data["instance_id"], singleton_lock._INSTANCE_ID)
        self.assertLess(time.time() - new_data["updated_at"], 5)

    def test_corrupted_heartbeat_file_does_not_crash_the_check(self):
        singleton_lock.HEARTBEAT_FILE.write_text("not valid json {{{")
        claimed, message = singleton_lock.check_and_claim_lock()
        self.assertTrue(claimed, "a corrupted/unreadable heartbeat must be treated like a missing one")

    def test_release_on_clean_shutdown_removes_this_instances_own_heartbeat(self):
        singleton_lock.check_and_claim_lock()
        self.assertTrue(singleton_lock.HEARTBEAT_FILE.exists())
        singleton_lock.release_lock_on_clean_shutdown()
        self.assertFalse(singleton_lock.HEARTBEAT_FILE.exists())

    def test_release_does_not_remove_a_different_instances_heartbeat(self):
        """Guards against a race: if another (newer) instance somehow
        already claimed the lock by the time this one is shutting down,
        releasing must not delete THEIR heartbeat out from under them."""
        singleton_lock.HEARTBEAT_FILE.write_text(json.dumps({
            "instance_id": "someone-elses-instance", "hostname": "h", "updated_at": time.time(),
        }))
        singleton_lock.release_lock_on_clean_shutdown()
        self.assertTrue(singleton_lock.HEARTBEAT_FILE.exists(),
                        "must not delete a heartbeat that isn't this instance's own")

    async def test_heartbeat_loop_keeps_refreshing_the_timestamp(self):
        import asyncio
        singleton_lock.REFRESH_INTERVAL_SECONDS = 0.01  # speed up for the test
        singleton_lock.check_and_claim_lock()
        first = json.loads(singleton_lock.HEARTBEAT_FILE.read_text())["updated_at"]

        task = asyncio.create_task(singleton_lock.heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()

        second = json.loads(singleton_lock.HEARTBEAT_FILE.read_text())["updated_at"]
        self.assertGreater(second, first, "the background loop must actually refresh the timestamp")


if __name__ == "__main__":
    unittest.main()
