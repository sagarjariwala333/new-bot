"""
test_default_settings.py
========================

Tests for the "nothing is pre-filled" design:

  * every strategy / sizing / risk / tracker / operational setting is BLANK
    in the shipped code (no defaults anywhere);
  * the Default Settings store: validation, Save (marks unconfirmed),
    Confirm (refuses while anything is blank), persistence;
  * a new pair can only be created from CONFIRMED defaults;
  * a pair with any blank setting can never be started (Binance and OKX),
    and a refused restart never stops a running instance;
  * the live-trading switch (OFF by default; testnet/demo accounts exempt);
  * the alert email lives on the server, not in source;
  * the dashboard has a field for every setting and ships no pre-filled values.

HONESTY NOTE: pydantic / fastapi / aiohttp are stand-ins in this sandbox (see
tests/_pydantic_stub.py), so pydantic's own range checks are NOT exercised here.
The range / blank checks tested below are the ones in app/default_settings.py,
which run in the real app too and are the server's authoritative check.
"""

import asyncio
import dataclasses
import json
import os
import re
import shutil
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_default_settings")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

import app.default_settings as ds  # noqa: E402
import app.store as store_module  # noqa: E402
import app.okx_store as okx_store_module  # noqa: E402
import app.manager as manager_module  # noqa: E402
import app.okx_manager as okx_manager_module  # noqa: E402
import app.api as api  # noqa: E402
import app.api.okx as okx_api  # noqa: E402
from app import strategy as strat  # noqa: E402
from app import base_v3_tracker  # noqa: E402
from app import singleton_lock  # noqa: E402
from app.api.schemas import PairCreate, OKXPairCreate  # noqa: E402
from app.instance import _params_from_pair  # noqa: E402
from tests import _test_values as tv  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"

NON_SETTING_FIELDS = {"symbol", "enabled", "frozen"}


def full_values(platform: str) -> dict:
    vals = {**tv.STRATEGY_VALUES, **tv.PAIR_EXTRA}
    if platform == "okx":
        vals["trigger_px_type"] = "mark"
    return vals


def fresh_store(name="default_settings.json") -> ds.DefaultSettingsStore:
    path = TEST_DATA_DIR / name
    path.unlink(missing_ok=True)
    return ds.DefaultSettingsStore(path=path)


# --------------------------------------------------------------------------
class TestNothingIsPrefilledInCode(unittest.TestCase):
    def test_pair_configs_have_no_default_setting_values(self):
        for cls in (store_module.PairConfig, okx_store_module.OKXPairConfig):
            for f in dataclasses.fields(cls):
                if f.name in NON_SETTING_FIELDS:
                    continue
                self.assertIs(f.default, None, f"{cls.__name__}.{f.name} must default to blank (None)")

    def test_a_new_pair_config_is_entirely_blank(self):
        pc = store_module.PairConfig(symbol="TESTUSDT")
        okx = okx_store_module.OKXPairConfig(symbol="TEST-USDT-SWAP")
        for obj in (pc, okx):
            self.assertFalse(obj.enabled)
            for f in dataclasses.fields(obj):
                if f.name not in NON_SETTING_FIELDS and f.name != "symbol":
                    self.assertIsNone(getattr(obj, f.name), f.name)

    def test_strategy_params_cannot_be_built_without_every_value(self):
        with self.assertRaises(TypeError):
            strat.StrategyParams()
        with self.assertRaises(TypeError):
            strat.StrategyParams(**{k: v for k, v in tv.STRATEGY_VALUES.items() if k != "leverage"})

    def test_pair_create_schemas_carry_no_defaults(self):
        for schema in (PairCreate, OKXPairCreate):
            body = schema(symbol="TESTUSDT" if schema is PairCreate else "TEST-USDT-SWAP")
            dumped = body.model_dump()
            for name in ds.fields_for("okx" if schema is OKXPairCreate else "binance"):
                self.assertIsNone(dumped.get(name), f"{schema.__name__}.{name}")

    def test_tracker_refuses_a_blank_start_balance(self):
        base_v3_tracker._path("binance", "acc_blank", "TESTUSDT").unlink(missing_ok=True)
        for blank in (None, 0, 0.0):
            with self.assertRaises(ValueError):
                base_v3_tracker.load("binance", "acc_blank", "TESTUSDT", blank)

    def test_params_from_pair_refuses_blank_settings_and_names_them(self):
        pc = tv.pair_config(hma_length=None, leverage=None)
        with self.assertRaises(RuntimeError) as cm:
            _params_from_pair(pc)
        self.assertIn("hma_length", str(cm.exception))
        self.assertIn("leverage", str(cm.exception))
        self.assertIsInstance(_params_from_pair(tv.pair_config()), strat.StrategyParams)

    def test_every_setting_is_covered_by_the_default_settings_form(self):
        binance_fields = {f.name for f in dataclasses.fields(store_module.PairConfig)} - NON_SETTING_FIELDS
        okx_fields = {f.name for f in dataclasses.fields(okx_store_module.OKXPairConfig)} - NON_SETTING_FIELDS
        self.assertEqual(set(ds.fields_for("binance")), binance_fields)
        self.assertEqual(set(ds.fields_for("okx")), okx_fields)
        self.assertEqual(okx_fields - binance_fields, {"trigger_px_type"})


# --------------------------------------------------------------------------
class TestValidation(unittest.TestCase):
    def v(self, name, value, platform="binance"):
        return ds.validate_value(platform, name, value)

    def test_blank_is_never_accepted(self):
        for blank in (None, "", "   "):
            with self.assertRaises(ds.SettingsError):
                self.v("hma_length", blank)

    def test_numeric_ranges(self):
        self.assertEqual(self.v("hma_length", 5), 5)
        for bad in (0, -1, 501, 2.5, "abc", float("nan"), float("inf"), True):
            with self.assertRaises(ds.SettingsError, msg=repr(bad)):
                self.v("hma_length", bad)
        with self.assertRaises(ds.SettingsError):
            self.v("tracker_start_balance", 0)          # lower bound is exclusive
        self.assertEqual(self.v("tracker_start_balance", 0.01), 0.01)
        with self.assertRaises(ds.SettingsError):
            self.v("hold_adx_level", 100.01)
        self.assertEqual(self.v("hold_adx_level", 0), 0)  # lower bound inclusive here

    def test_leverage_must_be_a_whole_number(self):
        self.assertEqual(self.v("leverage", 3), 3.0)
        for bad in (2.5, 0, 126):
            with self.assertRaises(ds.SettingsError):
                self.v("leverage", bad)

    def test_booleans_must_be_real_booleans(self):
        self.assertIs(self.v("use_shadow", True), True)
        self.assertIs(self.v("use_shadow", False), False)
        for bad in ("true", 1, 0, "yes"):
            with self.assertRaises(ds.SettingsError):
                self.v("use_shadow", bad)

    def test_timeframe_sets_differ_by_platform(self):
        self.assertEqual(self.v("timeframe", "8h", "binance"), "8h")
        with self.assertRaises(ds.SettingsError):
            self.v("timeframe", "8h", "okx")
        self.assertEqual(self.v("timeframe", "12h", "okx"), "12h")
        with self.assertRaises(ds.SettingsError):
            self.v("timeframe", "7h", "binance")

    def test_trigger_type_is_okx_only(self):
        self.assertEqual(self.v("trigger_px_type", "last", "okx"), "last")
        with self.assertRaises(ds.SettingsError):
            self.v("trigger_px_type", "bogus", "okx")
        with self.assertRaises(ds.SettingsError):
            self.v("trigger_px_type", "mark", "binance")

    def test_unknown_field_or_platform(self):
        with self.assertRaises(ds.SettingsError):
            self.v("not_a_setting", 1)
        with self.assertRaises(ds.SettingsError):
            ds.fields_for("kraken")

    def test_missing_fields_lists_every_blank_or_invalid_one(self):
        vals = full_values("binance")
        self.assertEqual(ds.missing_fields("binance", vals), [])
        vals["leverage"] = None
        vals["hma_length"] = 0
        self.assertEqual(sorted(ds.missing_fields("binance", vals)), ["hma_length", "leverage"])

    def test_email_validation(self):
        self.assertEqual(ds.validate_email("  me@example.com "), "me@example.com")
        self.assertEqual(ds.validate_email(""), "")
        for bad in ("nope", "a@b", "a b@c.com", "@x.com"):
            with self.assertRaises(ds.SettingsError):
                ds.validate_email(bad)


# --------------------------------------------------------------------------
class TestDefaultSettingsStore(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()

    def test_fresh_store_is_blank_and_unconfirmed(self):
        for plat in ds.PLATFORMS:
            g = self.s.get(plat)
            self.assertFalse(g["confirmed"])
            self.assertTrue(all(v is None for v in g["values"].values()))
            self.assertEqual(sorted(g["missing"]), sorted(ds.fields_for(plat)))
            with self.assertRaises(ds.DefaultsNotConfirmed):
                self.s.confirmed_values(plat)
        self.assertEqual(self.s.get_alert_email(), "")
        self.assertFalse(self.s.live_trading_enabled())

    def test_save_is_atomic_when_any_value_is_invalid(self):
        with self.assertRaises(ds.SettingsError) as cm:
            self.s.save("binance", {"hma_length": 5, "leverage": 2.5, "atr_length": 0})
        self.assertIn("leverage", str(cm.exception))
        self.assertIn("atr_length", str(cm.exception))
        self.assertIsNone(self.s.get("binance")["values"]["hma_length"], "nothing may be stored on error")

    def test_save_rejects_a_field_from_the_other_platform(self):
        with self.assertRaises(ds.SettingsError):
            self.s.save("binance", {"trigger_px_type": "mark"})
        self.s.save("okx", {"trigger_px_type": "mark"})

    def test_cannot_confirm_while_anything_is_blank(self):
        vals = full_values("binance")
        vals.pop("leverage")
        self.s.save("binance", vals)
        with self.assertRaises(ds.SettingsError) as cm:
            self.s.confirm("binance")
        self.assertIn("leverage", str(cm.exception))
        self.assertFalse(self.s.is_confirmed("binance"))

    def test_confirm_then_edit_goes_back_to_unconfirmed(self):
        self.s.save("binance", full_values("binance"))
        g = self.s.confirm("binance")
        self.assertTrue(g["confirmed"])
        self.assertTrue(g["confirmed_at"].endswith("SGT"))
        self.assertEqual(self.s.confirmed_values("binance")["hma_length"], tv.STRATEGY_VALUES["hma_length"])
        self.s.save("binance", {"hma_length": 6})
        self.assertFalse(self.s.is_confirmed("binance"))
        with self.assertRaises(ds.DefaultsNotConfirmed):
            self.s.confirmed_values("binance")

    def test_blank_clears_a_field(self):
        self.s.save("binance", full_values("binance"))
        self.s.save("binance", {"leverage": None})
        self.assertIsNone(self.s.get("binance")["values"]["leverage"])
        self.assertIn("leverage", self.s.get("binance")["missing"])

    def test_platforms_are_independent(self):
        self.s.save("binance", full_values("binance"))
        self.s.confirm("binance")
        self.assertTrue(self.s.is_confirmed("binance"))
        self.assertFalse(self.s.is_confirmed("okx"))
        with self.assertRaises(ds.DefaultsNotConfirmed):
            self.s.confirmed_values("okx")

    def test_okx_needs_its_trigger_type_to_confirm(self):
        vals = full_values("okx")
        vals.pop("trigger_px_type")
        self.s.save("okx", vals)
        with self.assertRaises(ds.SettingsError):
            self.s.confirm("okx")
        self.s.save("okx", {"trigger_px_type": "index"})
        self.s.confirm("okx")

    def test_everything_persists_across_a_restart(self):
        self.s.save("binance", full_values("binance"))
        self.s.confirm("binance")
        self.s.set_alert_email("me@example.com")
        self.s.set_live_trading_enabled(True)
        again = ds.DefaultSettingsStore(path=TEST_DATA_DIR / "default_settings.json")
        self.assertTrue(again.is_confirmed("binance"))
        self.assertEqual(again.confirmed_values("binance"), self.s.confirmed_values("binance"))
        self.assertEqual(again.get_alert_email(), "me@example.com")
        self.assertTrue(again.live_trading_enabled())

    def test_a_corrupt_file_falls_back_to_blank_never_to_invented_values(self):
        path = TEST_DATA_DIR / "corrupt.json"
        path.write_text("{ this is not json")
        s = ds.DefaultSettingsStore(path=path)
        self.assertFalse(s.is_confirmed("binance"))
        self.assertTrue(all(v is None for v in s.get("binance")["values"].values()))
        self.assertFalse(s.live_trading_enabled())

    def test_a_hand_edited_file_that_claims_confirmed_but_is_incomplete_is_not_trusted(self):
        path = TEST_DATA_DIR / "tampered.json"
        path.write_text(json.dumps({"binance": {"values": {"hma_length": 5}, "confirmed": True}}))
        s = ds.DefaultSettingsStore(path=path)
        with self.assertRaises(ds.DefaultsNotConfirmed):
            s.confirmed_values("binance")

    def test_settings_file_is_not_world_readable(self):
        self.s.save("binance", {"hma_length": 5})
        mode = (TEST_DATA_DIR / "default_settings.json").stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, oct(mode))

    def test_build_pair_values_uses_confirmed_defaults_and_validates_overrides(self):
        self.s.save("binance", full_values("binance"))
        self.s.confirm("binance")
        with mock.patch.object(ds, "default_settings", self.s):
            merged = ds.build_pair_values("binance", {"leverage": 2, "hma_length": None})
            self.assertEqual(merged["leverage"], 2.0)
            self.assertEqual(merged["hma_length"], tv.STRATEGY_VALUES["hma_length"])
            with self.assertRaises(ds.SettingsError):
                ds.build_pair_values("binance", {"leverage": 2.5})
        self.s.save("binance", {"hma_length": 7})   # now unconfirmed
        with mock.patch.object(ds, "default_settings", self.s):
            with self.assertRaises(ds.DefaultsNotConfirmed):
                ds.build_pair_values("binance", {})


# --------------------------------------------------------------------------
class TestLiveTradingSwitch(unittest.TestCase):
    def test_off_by_default_and_blocks_real_accounts_only(self):
        s = fresh_store("live.json")
        with mock.patch.object(ds, "default_settings", s):
            ds.require_may_trade("binance", True)           # testnet always allowed
            ds.require_may_trade("okx", True)               # demo always allowed
            with self.assertRaises(RuntimeError) as cm:
                ds.require_may_trade("binance", False)
            self.assertIn("switched OFF", str(cm.exception))
            s.set_live_trading_enabled(True)
            ds.require_may_trade("binance", False)
            s.set_live_trading_enabled(False)
            with self.assertRaises(RuntimeError):
                ds.require_may_trade("okx", False)


# --------------------------------------------------------------------------
class _ManagerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR / "mgr", ignore_errors=True)
        (TEST_DATA_DIR / "mgr").mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "mgr" / "accounts.json"
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "mgr" / "okx_accounts.json"
        self.store = store_module.Store()
        self.okx_store = okx_store_module.OKXStore()
        manager_module.store = self.store
        okx_manager_module.okx_store = self.okx_store
        self.switch = fresh_store("mgr_switch.json")
        self._patch = mock.patch.object(ds, "default_settings", self.switch)
        self._patch.start()
        singleton_lock._lock_held = True

    def tearDown(self):
        self._patch.stop()
        singleton_lock._lock_held = True


class TestBinanceStartGates(_ManagerBase):
    def setUp(self):
        super().setUp()
        self.mgr = manager_module.BotManager()
        self.mgr.store = self.store
        self.test_acc = self.store.create_account("T", "k", "s", True)     # testnet
        self.real_acc = self.store.create_account("R", "k", "s", False)    # real money

    async def test_blank_pair_cannot_start_and_names_the_blank_fields(self):
        self.store.add_pair(self.test_acc.id, store_module.PairConfig(symbol="BLANKUSDT"))
        with mock.patch.object(self.mgr, "_build_instance") as build:
            with self.assertRaises(RuntimeError) as cm:
                await self.mgr.start(self.test_acc.id, "BLANKUSDT")
            build.assert_not_called()
        self.assertIn("blank or invalid", str(cm.exception))
        self.assertIn("hma_length", str(cm.exception))
        self.assertNotIn("BLANKUSDT", self.mgr.instances)

    async def test_every_single_blank_field_blocks_the_start(self):
        for name in ds.fields_for("binance"):
            pc = tv.pair_config(symbol="ONEUSDT", **{name: None})
            self.store.delete_pair(self.test_acc.id, "ONEUSDT")
            self.store.add_pair(self.test_acc.id, pc)
            with self.assertRaises(RuntimeError, msg=f"blank {name} must block start") as cm:
                self.mgr._check_may_start(self.test_acc.id, "ONEUSDT")
            self.assertIn(name, str(cm.exception))

    async def test_complete_pair_on_testnet_is_allowed_even_with_live_switch_off(self):
        self.store.add_pair(self.test_acc.id, tv.pair_config(symbol="OKUSDT"))
        self.mgr._check_may_start(self.test_acc.id, "OKUSDT")   # must not raise

    async def test_complete_pair_on_a_real_account_needs_the_live_switch(self):
        self.store.add_pair(self.real_acc.id, tv.pair_config(symbol="REALUSDT"))
        with mock.patch.object(self.mgr, "_build_instance") as build:
            with self.assertRaises(RuntimeError) as cm:
                await self.mgr.start(self.real_acc.id, "REALUSDT")
            build.assert_not_called()
        self.assertIn("switched OFF", str(cm.exception))
        self.switch.set_live_trading_enabled(True)
        self.mgr._check_may_start(self.real_acc.id, "REALUSDT")

    async def test_refused_restart_does_not_stop_a_running_instance(self):
        self.store.add_pair(self.real_acc.id, tv.pair_config(symbol="RUNUSDT", enabled=True))
        running = mock.MagicMock()
        running.stop = mock.AsyncMock()
        self.mgr.instances[manager_module._key(self.real_acc.id, "RUNUSDT")] = running
        with self.assertRaises(RuntimeError):
            await self.mgr.restart(self.real_acc.id, "RUNUSDT")     # live switch is OFF
        running.stop.assert_not_called()
        self.assertTrue(self.store.get_account(self.real_acc.id).pairs["RUNUSDT"].enabled,
                        "a refused restart must not flip the enabled flag")

    async def test_start_all_enabled_skips_blank_pairs_without_crashing(self):
        self.store.add_pair(self.test_acc.id, store_module.PairConfig(symbol="BLANKUSDT", enabled=True))
        await self.mgr.start_all_enabled()      # must not raise
        self.assertNotIn(manager_module._key(self.test_acc.id, "BLANKUSDT"), self.mgr.instances)


class TestOKXStartGates(_ManagerBase):
    def setUp(self):
        super().setUp()
        self.mgr = okx_manager_module.OKXBotManager()
        self.mgr.store = self.okx_store
        self.demo_acc = self.okx_store.create_account("D", "k", "s", "p", demo=True)
        self.real_acc = self.okx_store.create_account("R", "k", "s", "p", demo=False)

    async def test_blank_pair_cannot_start(self):
        self.okx_store.add_pair(self.demo_acc.id, okx_store_module.OKXPairConfig(symbol="AAA-USDT-SWAP"))
        with self.assertRaises(RuntimeError) as cm:
            await self.mgr.start(self.demo_acc.id, "AAA-USDT-SWAP")
        self.assertIn("blank or invalid", str(cm.exception))

    async def test_trigger_type_is_required_on_okx(self):
        self.okx_store.add_pair(self.demo_acc.id, tv.okx_pair_config(symbol="BBB-USDT-SWAP", trigger_px_type=None))
        with self.assertRaises(RuntimeError) as cm:
            self.mgr._check_may_start(self.demo_acc.id, "BBB-USDT-SWAP")
        self.assertIn("trigger_px_type", str(cm.exception))

    async def test_demo_allowed_real_needs_the_switch(self):
        self.okx_store.add_pair(self.demo_acc.id, tv.okx_pair_config(symbol="CCC-USDT-SWAP"))
        self.okx_store.add_pair(self.real_acc.id, tv.okx_pair_config(symbol="DDD-USDT-SWAP"))
        self.mgr._check_may_start(self.demo_acc.id, "CCC-USDT-SWAP")
        with self.assertRaises(RuntimeError) as cm:
            await self.mgr.start(self.real_acc.id, "DDD-USDT-SWAP")
        self.assertIn("switched OFF", str(cm.exception))
        self.switch.set_live_trading_enabled(True)
        self.mgr._check_may_start(self.real_acc.id, "DDD-USDT-SWAP")

    async def test_refused_restart_does_not_stop_a_running_instance(self):
        self.okx_store.add_pair(self.real_acc.id, tv.okx_pair_config(symbol="EEE-USDT-SWAP", enabled=True))
        running = mock.MagicMock()
        running.stop = mock.AsyncMock()
        self.mgr.instances[okx_manager_module._key(self.real_acc.id, "EEE-USDT-SWAP")] = running
        with self.assertRaises(RuntimeError):
            await self.mgr.restart(self.real_acc.id, "EEE-USDT-SWAP")
        running.stop.assert_not_called()


# --------------------------------------------------------------------------
class TestApiRoutes(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR / "api", ignore_errors=True)
        (TEST_DATA_DIR / "api").mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "api" / "accounts.json"
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "api" / "okx_accounts.json"
        self.store = store_module.Store()
        self.okx_store = okx_store_module.OKXStore()
        api.store = self.store
        okx_api.okx_store = self.okx_store
        self.switch = fresh_store("api_switch.json")
        self._patch = mock.patch.object(ds, "default_settings", self.switch)
        self._patch.start()
        self.acc = self.store.create_account("T", "k", "s", True)
        self.okx_acc = self.okx_store.create_account("D", "k", "s", "p", demo=True)

    def tearDown(self):
        self._patch.stop()

    def test_get_default_settings_is_blank_on_a_fresh_install(self):
        d = api.get_default_settings()
        for plat in ("binance", "okx"):
            self.assertFalse(d[plat]["confirmed"])
            self.assertTrue(all(v is None for v in d[plat]["values"].values()))
        self.assertEqual(d["alert_email"], "")
        self.assertFalse(d["live_trading_enabled"])

    def test_add_pair_is_refused_until_defaults_are_confirmed(self):
        with self.assertRaises(Exception) as cm:
            api.add_pair(self.acc.id, PairCreate(symbol="TESTUSDT"))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.store.get_account(self.acc.id).pairs, {})
        with self.assertRaises(Exception) as cm:
            okx_api.add_okx_pair(self.okx_acc.id, OKXPairCreate(symbol="TEST-USDT-SWAP"))
        self.assertEqual(cm.exception.status_code, 409)

    def test_full_flow_save_confirm_then_add_pair(self):
        class Body:
            values = full_values("binance")
        saved = api.save_default_settings("binance", Body)
        self.assertFalse(saved["confirmed"])
        self.assertEqual(saved["missing"], [])
        with self.assertRaises(Exception) as cm:               # saved but not confirmed yet
            api.add_pair(self.acc.id, PairCreate(symbol="TESTUSDT"))
        self.assertEqual(cm.exception.status_code, 409)

        confirmed = api.confirm_default_settings("binance")
        self.assertTrue(confirmed["confirmed"])

        created = api.add_pair(self.acc.id, PairCreate(symbol="TESTUSDT"))
        for name, value in full_values("binance").items():
            self.assertEqual(created[name], value, name)
        self.assertFalse(created["enabled"])                  # creating a pair never starts it

    def test_add_pair_override_is_validated_and_applied(self):
        class Body:
            values = full_values("binance")
        api.save_default_settings("binance", Body)
        api.confirm_default_settings("binance")
        created = api.add_pair(self.acc.id, PairCreate(symbol="AAAUSDT", leverage=2))
        self.assertEqual(created["leverage"], 2.0)
        # A bad value is refused either by the server's own check (HTTP 400, used with the
        # stand-in pydantic) or - with the REAL pydantic - already when the request object is
        # built (which a real server answers with HTTP 422). Either way the pair is not created.
        with self.assertRaises(Exception) as cm:
            api.add_pair(self.acc.id, PairCreate(symbol="BBBUSDT", leverage=2.5))
        self.assertTrue(getattr(cm.exception, "status_code", None) == 400
                        or type(cm.exception).__name__ == "ValidationError", repr(cm.exception))
        self.assertNotIn("BBBUSDT", self.store.get_account(self.acc.id).pairs)

    def test_confirm_refuses_incomplete_settings_with_a_400(self):
        class Body:
            values = {k: v for k, v in full_values("binance").items() if k != "leverage"}
        api.save_default_settings("binance", Body)
        with self.assertRaises(Exception) as cm:
            api.confirm_default_settings("binance")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("leverage", cm.exception.detail)

    def test_save_with_an_out_of_range_value_is_a_400_and_stores_nothing(self):
        class Body:
            values = {"hma_length": 0}
        with self.assertRaises(Exception) as cm:
            api.save_default_settings("binance", Body)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIsNone(self.switch.get("binance")["values"]["hma_length"])

    def test_unknown_platform_is_a_404(self):
        class Body:
            values = {}
        for fn, args in ((api.save_default_settings, ("kraken", Body)), (api.confirm_default_settings, ("kraken",))):
            with self.assertRaises(Exception) as cm:
                fn(*args)
            self.assertEqual(cm.exception.status_code, 404)

    def test_okx_flow_includes_the_trigger_type(self):
        class Body:
            values = full_values("okx")
        api.save_default_settings("okx", Body)
        api.confirm_default_settings("okx")
        created = okx_api.add_okx_pair(self.okx_acc.id, OKXPairCreate(symbol="TEST-USDT-SWAP"))
        self.assertEqual(created["trigger_px_type"], "mark")
        self.assertEqual(created["timeframe"], tv.PAIR_EXTRA["timeframe"])

    def test_alert_email_and_live_switch_routes(self):
        class E:
            email = "me@example.com"
        self.assertEqual(api.save_alert_email(E)["alert_email"], "me@example.com")
        class Bad:
            email = "not an email"
        with self.assertRaises(Exception) as cm:
            api.save_alert_email(Bad)
        self.assertEqual(cm.exception.status_code, 400)
        class On:
            enabled = True
        self.assertTrue(api.set_live_trading(On)["live_trading_enabled"])
        class Off:
            enabled = False
        self.assertFalse(api.set_live_trading(Off)["live_trading_enabled"])


# --------------------------------------------------------------------------
class TestDashboardFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def _field_meta_keys(self):
        block = self.html[self.html.index("const FIELD_META = {"): self.html.index("const TIMEFRAMES")]
        return set(re.findall(r"^\s{2}(\w+):\s*\{", block, flags=re.M))

    def test_dashboard_has_a_field_for_every_setting(self):
        keys = self._field_meta_keys()
        for name in ds.fields_for("binance"):
            self.assertIn(name, keys, f"dashboard FIELD_META is missing {name}")
        self.assertIn("trigger_px_type", self.html)       # OKX-only select, built separately
        for name in ds.fields_for("okx"):
            self.assertTrue(name in keys or name == "trigger_px_type")

    def test_dashboard_has_no_built_in_defaults(self):
        self.assertNotIn("BASE_V3_DEFAULTS", self.html)
        block = self.html[self.html.index("const FIELD_META = {"): self.html.index("const TIMEFRAMES")]
        self.assertNotRegex(block, r"value\s*[:=]\s*[\"']?\d")
        for m in re.finditer(r'<input id="(?:wf|mc)-[a-z]+"[^>]*>', self.html):
            self.assertNotIn("value=", m.group(0), f"analysis input must start blank: {m.group(0)}")
        self.assertNotRegex(self.html, r"placeholder=\"e\.g\. \d")

    def test_default_settings_page_and_confirm_button_exist(self):
        self.assertIn('id="tabpage-defaults"', self.html)
        self.assertIn("Confirm Default Settings", self.html)
        self.assertIn("/api/default-settings", self.html)
        self.assertIn('id="ds-live"', self.html)
        self.assertIn('id="ds-email"', self.html)

    def test_add_pair_requires_confirmed_defaults(self):
        self.assertIn("confirmedDefaultsOrWarn('binance')", self.html)
        self.assertIn("confirmedDefaultsOrWarn('okx')", self.html)


if __name__ == "__main__":
    unittest.main()
