"""
test_h1_h2_fixes.py
===================

Tests for the two pre-live audit fixes.

H1  Startup lock after a Railway restart/redeploy
    * a fresh heartbeat from another instance blocks trading in THIS process (unchanged),
    * but this process now keeps retrying and starts trading by itself once the other
      heartbeat is gone / stale - nobody has to restart it,
    * it never starts trading while the other instance still looks alive,
    * an alert is sent once if it is still blocked after the alert delay; an alert failure
      never stops the retry loop,
    * the retry loop is cancelled cleanly on shutdown,
    * the retry does not flood the log.

H2  Tracker / persistent storage
    * on Railway, trading is refused when DATA_DIR is not on the attached volume,
    * an unreadable tracker is restored from its last good backup, or the pair refuses to
      start - it is NEVER silently restarted from the starting amount,
    * the tracker's all-time high survives all of that,
    * railway.json carries the shutdown-time setting as a NUMBER.

HONESTY NOTE: Railway itself is not available here; its variables and its volume are
simulated. These tests prove the code's logic, not Railway's behaviour.
"""

import asyncio
import json
import os
import shutil
import sys
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_h1_h2")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

import app.main as main  # noqa: E402
import app.singleton_lock as singleton_lock  # noqa: E402
import app.startup_guard as sg  # noqa: E402
import app.base_v3_tracker as tracker  # noqa: E402
import app.store as store_module  # noqa: E402
import app.okx_store as okx_store_module  # noqa: E402
import app.manager as manager_module  # noqa: E402
import app.okx_manager as okx_manager_module  # noqa: E402
import app.default_settings as ds  # noqa: E402
from tests import _test_values as tv  # noqa: E402

RAILWAY = {"RAILWAY_PROJECT_ID": "p", "RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_SERVICE_ID": "s"}
ROOT_DEV, VOLUME_DEV = 1, 2


def devices(mapping):
    """device_of replacement: path -> fake filesystem id."""
    def device_of(path):
        path = str(path)
        best = None
        for prefix, dev in mapping.items():
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, dev)
        return best[1] if best else None
    return device_of


# ============================================================ H2: storage guard
class TestStorageGuard(unittest.TestCase):
    def test_off_railway_the_check_does_nothing(self):
        self.assertIsNone(sg.check_storage(Path("/anywhere/data"), env={}))
        self.assertFalse(sg.running_on_railway({}))

    def test_any_one_railway_marker_means_railway(self):
        for key in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_ID"):
            self.assertTrue(sg.running_on_railway({key: "x"}), key)

    def test_data_dir_inside_the_volume_is_accepted(self):
        env = {**RAILWAY, "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
        self.assertIsNone(sg.check_storage(Path("/data"), env=env))
        self.assertIsNone(sg.check_storage(Path("/data/bot"), env=env))

    def test_data_dir_outside_the_volume_is_refused(self):
        env = {**RAILWAY, "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
        problem = sg.check_storage(Path("/app/data"), env=env)
        self.assertIsNotNone(problem)
        self.assertIn("/data", problem)
        self.assertIn("lost on every redeploy", problem)

    def test_a_sibling_path_with_the_same_prefix_is_not_inside_the_volume(self):
        env = {**RAILWAY, "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
        self.assertIsNotNone(sg.check_storage(Path("/data2"), env=env))
        self.assertIsNotNone(sg.check_storage(Path("/database"), env=env))

    def test_no_volume_variable_and_container_disk_is_refused(self):
        dev = devices({"/": ROOT_DEV})
        problem = sg.check_storage(Path("/app/data"), env=RAILWAY, device_of=dev)
        self.assertIsNotNone(problem)
        self.assertIn("Add a Volume", problem)

    def test_no_volume_variable_but_separate_filesystem_is_accepted(self):
        dev = devices({"/": ROOT_DEV, "/data": VOLUME_DEV})
        self.assertIsNone(sg.check_storage(Path("/data"), env=RAILWAY, device_of=dev))

    def test_unknown_filesystem_fails_safe(self):
        self.assertIsNotNone(sg.check_storage(Path("/data"), env=RAILWAY, device_of=lambda p: None))

    def test_override_needs_the_exact_phrase(self):
        dev = devices({"/": ROOT_DEV})
        for wrong in ("true", "1", "yes", "i_understand_the_tracker_will_be_lost", ""):
            env = {**RAILWAY, sg.OVERRIDE_VAR: wrong}
            self.assertIsNotNone(sg.check_storage(Path("/app/data"), env=env, device_of=dev), wrong)
        env = {**RAILWAY, sg.OVERRIDE_VAR: sg.OVERRIDE_PHRASE}
        self.assertIsNone(sg.check_storage(Path("/app/data"), env=env, device_of=dev))

    def test_require_storage_ok_follows_the_recorded_result(self):
        try:
            sg.set_problem_for_tests(None)
            sg.require_storage_ok()
            sg.set_problem_for_tests("DATA_DIR is on the container's own disk")
            with self.assertRaises(RuntimeError) as cm:
                sg.require_storage_ok()
            self.assertIn("Refusing to start", str(cm.exception))
        finally:
            sg.set_problem_for_tests(None)


class TestManagersHonourTheStorageGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR / "mgr", ignore_errors=True)
        (TEST_DATA_DIR / "mgr").mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "mgr" / "accounts.json"
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "mgr" / "okx_accounts.json"
        self.store, self.okx_store = store_module.Store(), okx_store_module.OKXStore()
        manager_module.store, okx_manager_module.okx_store = self.store, self.okx_store
        self.mgr = manager_module.BotManager()
        self.mgr.store = self.store
        self.okx = okx_manager_module.OKXBotManager()
        self.okx.store = self.okx_store
        self.acc = self.store.create_account("T", "k", "s", True)
        self.oacc = self.okx_store.create_account("D", "k", "s", "p", demo=True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="AAAUSDT"))
        self.okx_store.add_pair(self.oacc.id, tv.okx_pair_config(symbol="AAA-USDT-SWAP"))
        singleton_lock._lock_held = True

    def tearDown(self):
        sg.set_problem_for_tests(None)
        singleton_lock._lock_held = True

    async def test_binance_start_and_restart_are_refused_while_storage_is_not_persistent(self):
        sg.set_problem_for_tests("DATA_DIR is on the container's own disk")
        with mock.patch.object(self.mgr, "_build_instance") as build:
            with self.assertRaises(RuntimeError):
                await self.mgr.start(self.acc.id, "AAAUSDT")
            with self.assertRaises(RuntimeError):
                await self.mgr.restart(self.acc.id, "AAAUSDT")
            build.assert_not_called()

    async def test_okx_start_and_restart_are_refused_while_storage_is_not_persistent(self):
        sg.set_problem_for_tests("DATA_DIR is on the container's own disk")
        with mock.patch.object(self.okx, "_build_instance") as build:
            with self.assertRaises(RuntimeError):
                await self.okx.start(self.oacc.id, "AAA-USDT-SWAP")
            with self.assertRaises(RuntimeError):
                await self.okx.restart(self.oacc.id, "AAA-USDT-SWAP")
            build.assert_not_called()

    async def test_start_is_allowed_again_once_storage_is_fine(self):
        sg.set_problem_for_tests(None)
        self.mgr._check_may_start(self.acc.id, "AAAUSDT")
        self.okx._check_may_start(self.oacc.id, "AAA-USDT-SWAP")


# ============================================================ H2: tracker protection
class TestTrackerProtection(unittest.TestCase):
    def setUp(self):
        self.dir = TEST_DATA_DIR / "tracker"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self._orig = tracker.TRACKER_DIR
        tracker.TRACKER_DIR = self.dir
        self.path = tracker._path("binance", "acc", "TESTUSDT")

    def tearDown(self):
        tracker.TRACKER_DIR = self._orig

    def _make_tracker_with_a_high(self):
        st = tracker.load("binance", "acc", "TESTUSDT", 1000.0)
        st.balance, st.peak = 1730.0, 1730.0          # an all-time high worth protecting
        st.last_processed_candle = 123456
        tracker.save(st)
        tracker.save(st)                               # second save -> backup of a good file exists
        return st

    def test_a_missing_file_still_creates_a_fresh_tracker(self):
        st = tracker.load("binance", "acc", "TESTUSDT", 1000.0)
        self.assertEqual((st.balance, st.peak, st.start_balance), (1000.0, 1000.0, 1000.0))
        self.assertTrue(self.path.exists())

    def test_every_save_keeps_the_previous_good_file_as_a_backup(self):
        self._make_tracker_with_a_high()
        bak = tracker._backup_path(self.path)
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text())["peak"], 1730.0)

    def test_unreadable_file_is_restored_from_the_backup_and_keeps_the_high(self):
        self._make_tracker_with_a_high()
        self.path.write_text("{ this is not json")
        st = tracker.load("binance", "acc", "TESTUSDT", 1000.0)
        self.assertEqual(st.peak, 1730.0, "the all-time high must survive")
        self.assertEqual(st.balance, 1730.0)
        self.assertEqual(st.last_processed_candle, 123456)
        self.assertEqual(json.loads(self.path.read_text())["peak"], 1730.0, "the good copy is written back")
        self.assertTrue(list(self.dir.glob("*.unreadable-*")), "a copy of the bad file is kept for inspection")

    def test_unreadable_file_and_no_backup_refuses_instead_of_resetting(self):
        self._make_tracker_with_a_high()
        tracker._backup_path(self.path).unlink()
        self.path.write_text("{ this is not json")
        with self.assertRaises(tracker.TrackerUnreadableError) as cm:
            tracker.load("binance", "acc", "TESTUSDT", 1000.0)
        self.assertIn("will NOT start", str(cm.exception))
        self.assertEqual(self.path.read_text(), "{ this is not json", "the file is left exactly where it is")
        self.assertTrue(list(self.dir.glob("*.unreadable-*")))

    def test_unreadable_file_and_unreadable_backup_refuses(self):
        self._make_tracker_with_a_high()
        self.path.write_text("garbage")
        tracker._backup_path(self.path).write_text("also garbage")
        with self.assertRaises(tracker.TrackerUnreadableError):
            tracker.load("binance", "acc", "TESTUSDT", 1000.0)

    def test_a_bad_current_file_never_overwrites_a_good_backup(self):
        st = self._make_tracker_with_a_high()
        self.path.write_text("garbage")
        tracker.save(st)       # the writer saves while the on-disk file is bad
        self.assertEqual(json.loads(tracker._backup_path(self.path).read_text())["peak"], 1730.0)

    def test_deliberate_fresh_start_is_possible_by_deleting_both_files(self):
        self._make_tracker_with_a_high()
        self.path.write_text("garbage")
        tracker._backup_path(self.path).write_text("garbage")
        self.path.unlink()
        tracker._backup_path(self.path).unlink()
        st = tracker.load("binance", "acc", "TESTUSDT", 1000.0)
        self.assertEqual(st.peak, 1000.0)

    def test_changing_the_start_balance_still_rescales_and_keeps_the_ratio_to_the_high(self):
        self._make_tracker_with_a_high()
        st = tracker.load("binance", "acc", "TESTUSDT", 2000.0)
        self.assertAlmostEqual(st.peak / st.start_balance, 1.73, places=6)

    def test_a_pair_with_an_unreadable_tracker_cannot_be_built(self):
        from app.instance import BotInstance
        self._make_tracker_with_a_high()
        tracker._backup_path(self.path).unlink()
        self.path.write_text("garbage")
        pc = tv.pair_config(symbol="TESTUSDT")
        with self.assertRaises(tracker.TrackerUnreadableError):
            BotInstance(account_id="acc", account_name="T", symbol="TESTUSDT", api_key="k",
                        api_secret="s", testnet=True, pair_config=pc)


# ============================================================ H1: startup lock retry
class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR / "lock", ignore_errors=True)
        (TEST_DATA_DIR / "lock").mkdir(parents=True)
        singleton_lock.HEARTBEAT_FILE = TEST_DATA_DIR / "lock" / "heartbeat.json"
        self._saved = (main.CLAIM_RETRY_SECONDS, main.CONFLICT_ALERT_AFTER_SECONDS,
                       main._singleton_conflict_message, main._heartbeat_task, main._claim_task,
                       main._storage_problem_message)
        main.CLAIM_RETRY_SECONDS = 0.01
        main._singleton_conflict_message = None
        main._heartbeat_task = main._claim_task = None
        main._storage_problem_message = None
        sg.set_problem_for_tests(None)

    def tearDown(self):
        (main.CLAIM_RETRY_SECONDS, main.CONFLICT_ALERT_AFTER_SECONDS, main._singleton_conflict_message,
         main._heartbeat_task, main._claim_task, main._storage_problem_message) = self._saved
        singleton_lock._lock_held = True
        sg.set_problem_for_tests(None)

    @staticmethod
    def other_heartbeat(age_seconds):
        singleton_lock.HEARTBEAT_FILE.write_text(json.dumps({
            "instance_id": "old-deployment", "hostname": "old-host",
            "updated_at": time.time() - age_seconds}))

    def patched_engines(self):
        started = []

        async def fake_start():
            started.append(time.time())
        return started, mock.patch.object(main, "_start_trading_engines", fake_start)


class TestStartupLockRetry(_Base):
    async def test_fresh_other_heartbeat_blocks_then_start_happens_by_itself_when_it_goes_stale(self):
        self.other_heartbeat(age_seconds=1)               # old deployment looks alive
        started, p = self.patched_engines()
        with p, mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                await asyncio.sleep(0.1)
                self.assertEqual(started, [], "must NOT trade while the other instance looks alive")
                self.assertFalse(singleton_lock.is_lock_held())
                self.assertIn("Another instance", main._singleton_conflict_message)
                self.other_heartbeat(age_seconds=120)     # the old process is gone (heartbeat stale)
                for _ in range(100):
                    if started:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(started), 1, "trading starts by itself, exactly once")
                self.assertTrue(singleton_lock.is_lock_held())
                self.assertIsNone(main._singleton_conflict_message, "banner clears itself")
                await asyncio.sleep(0.1)
                self.assertEqual(len(started), 1, "never started twice")

    async def test_an_error_while_starting_the_engines_is_logged_not_lost(self):
        self.other_heartbeat(age_seconds=1)

        async def broken_start():
            raise RuntimeError("boom while starting")
        with mock.patch.object(main, "_start_trading_engines", broken_start), \
                mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                await asyncio.sleep(0.05)
                with self.assertLogs("main", level="ERROR") as logs:
                    self.other_heartbeat(age_seconds=120)
                    for _ in range(100):
                        if logs.records:
                            break
                        await asyncio.sleep(0.02)
                self.assertTrue(any("boom while starting" in (r.exc_text or "") or r.exc_info for r in logs.records))
                self.assertTrue(main._claim_task.done())

    async def test_clean_release_by_the_old_process_lets_the_new_one_start_quickly(self):
        self.other_heartbeat(age_seconds=1)
        started, p = self.patched_engines()
        with p, mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                await asyncio.sleep(0.05)
                self.assertEqual(started, [])
                singleton_lock.HEARTBEAT_FILE.unlink()    # what a clean shutdown of the old one does
                for _ in range(100):
                    if started:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(started), 1)

    async def test_still_alive_other_instance_is_never_overridden(self):
        self.other_heartbeat(age_seconds=1)               # the other instance is alive BEFORE we start
        started, p = self.patched_engines()
        with p:
            async with main.lifespan(main.app):
                for _ in range(15):
                    self.other_heartbeat(age_seconds=1)   # the other one keeps its heartbeat fresh
                    await asyncio.sleep(0.02)
                self.assertEqual(started, [])
                self.assertFalse(singleton_lock.is_lock_held())

    async def test_no_conflict_starts_immediately_without_a_retry_task(self):
        started, p = self.patched_engines()
        with p, mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                self.assertEqual(len(started), 1)
                self.assertIsNone(main._claim_task)
                self.assertIsNone(main._singleton_conflict_message)

    async def test_the_retry_task_is_cancelled_cleanly_on_shutdown(self):
        self.other_heartbeat(age_seconds=1)
        started, p = self.patched_engines()
        with p:
            async with main.lifespan(main.app):
                task = main._claim_task
                self.assertIsNotNone(task)
            self.assertTrue(task.cancelled() or task.done())
            self.assertEqual(started, [])

    async def test_a_conflict_is_logged_quietly_while_retrying(self):
        self.other_heartbeat(age_seconds=1)
        with self.assertNoLogs("singleton_lock", level="ERROR"):
            for _ in range(5):
                singleton_lock.check_and_claim_lock(quiet=True)
        with self.assertLogs("singleton_lock", level="ERROR"):
            singleton_lock.check_and_claim_lock()          # the first, non-quiet check still logs loudly


class TestStartupConflictAlert(_Base):
    async def test_alert_is_sent_once_after_the_delay_and_not_before(self):
        self.other_heartbeat(age_seconds=1)
        sent = []

        async def fake_notify(account, symbol, message, enabled):
            sent.append(message)
        started, p = self.patched_engines()
        main.CONFLICT_ALERT_AFTER_SECONDS = 3600
        with p, mock.patch.object(main.tg, "notify_error", fake_notify):
            async with main.lifespan(main.app):
                await asyncio.sleep(0.1)
                self.assertEqual(sent, [], "no alert before the delay")
                main.CONFLICT_ALERT_AFTER_SECONDS = 0
                for _ in range(100):
                    if sent:
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.15)
                self.assertEqual(len(sent), 1, "exactly one alert, not one per retry")
                self.assertIn("NOT started", sent[0])

    async def test_a_failing_alert_does_not_stop_the_retrying(self):
        self.other_heartbeat(age_seconds=1)

        async def broken_notify(*a, **k):
            raise RuntimeError("telegram is down")
        started, p = self.patched_engines()
        main.CONFLICT_ALERT_AFTER_SECONDS = 0
        with p, mock.patch.object(main.tg, "notify_error", broken_notify), \
                mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                await asyncio.sleep(0.15)
                self.other_heartbeat(age_seconds=120)
                for _ in range(100):
                    if started:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(started), 1, "still starts after the alert failed")


class TestStorageProblemAtStartup(_Base):
    async def test_no_lock_claim_and_no_engines_when_storage_is_not_persistent(self):
        started, p = self.patched_engines()
        alerts = []

        async def fake_notify(account, symbol, message, enabled):
            alerts.append(message)
        with p, mock.patch.object(sg, "check_storage", return_value="DATA_DIR is on the container's own disk"), \
                mock.patch.object(main.tg, "notify_error", fake_notify):
            async with main.lifespan(main.app):
                self.assertEqual(started, [])
                self.assertIsNone(main._claim_task)
                self.assertFalse(singleton_lock.HEARTBEAT_FILE.exists(), "must not even claim the lock")
                self.assertIn("container's own disk", main._storage_problem_message)
                self.assertEqual(len(alerts), 1)
                body = main.healthz()
                self.assertIn("storage_problem", json.dumps(body.content))
                with self.assertRaises(RuntimeError):
                    sg.require_storage_ok()

    async def test_healthz_stays_ok_and_reports_no_problem_when_all_is_well(self):
        started, p = self.patched_engines()
        with p, mock.patch.object(singleton_lock, "heartbeat_loop", new=lambda: asyncio.sleep(0)):
            async with main.lifespan(main.app):
                content = main.healthz().content
                self.assertEqual(content["status"], "ok")
                self.assertIsNone(content["storage_problem"])
                self.assertIsNone(content["singleton_lock_conflict"])


class TestPublicApiDocsAreOff(unittest.TestCase):
    """M4: /docs, /redoc and /openapi.json need no login, so they must be switched off.
    (The FastAPI used here is a stand-in, so this checks that the app is constructed with the
    documented switches that disable those pages, and that no route for them is registered.)"""

    def test_app_is_built_with_all_three_doc_pages_disabled(self):
        kw = main.app.init_kwargs
        for key in ("docs_url", "redoc_url", "openapi_url"):
            self.assertIn(key, kw, f"{key} must be passed explicitly")
            self.assertIsNone(kw[key], f"{key} must be None (= switched off)")

    def test_no_route_serves_the_docs(self):
        paths = {getattr(r, "path", "") for r in main.app.routes}
        for forbidden in ("/docs", "/redoc", "/openapi.json"):
            self.assertNotIn(forbidden, paths)

    def test_dashboard_and_health_check_are_still_served(self):
        paths = {getattr(r, "path", "") for r in main.app.routes}
        self.assertIn("/", paths)
        self.assertIn("/healthz", paths)


class TestRailwayConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((Path(__file__).resolve().parent.parent / "railway.json").read_text())["deploy"]

    def test_draining_seconds_is_a_number_not_a_string(self):
        self.assertIn("drainingSeconds", self.cfg)
        self.assertIsInstance(self.cfg["drainingSeconds"], int)
        self.assertNotIsInstance(self.cfg["drainingSeconds"], bool)
        self.assertGreaterEqual(self.cfg["drainingSeconds"], 60)

    def test_health_check_and_single_replica_are_kept(self):
        self.assertEqual(self.cfg["healthcheckPath"], "/healthz")
        self.assertEqual(self.cfg["numReplicas"], 1)
        self.assertEqual(self.cfg["startCommand"], "python run.py")


if __name__ == "__main__":
    unittest.main()
