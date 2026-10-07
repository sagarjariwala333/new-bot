import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_global_control")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402


class TestEmergencyFlattenAndStop(unittest.IsolatedAsyncioTestCase):
    """The actual action behind the STOP ALL button, at the single-instance
    level: must close any open position before pausing - unlike the plain
    stop() it calls afterward, which never closes anything by itself."""

    async def test_open_position_is_closed_before_stopping(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}

        await inst.emergency_flatten_and_stop()

        self.assertEqual(inst.state.status, "STOPPED")
        self.assertEqual(len(inst.client.close_orders), 1, "must have actually placed a close order")

    async def test_already_flat_instance_just_stops(self):
        inst = make_instance()
        inst.state.status = "IDLE"

        await inst.emergency_flatten_and_stop()

        self.assertEqual(inst.state.status, "STOPPED")
        self.assertEqual(len(inst.client.close_orders), 0, "nothing to close - must not place a spurious close order")

    async def test_reason_is_passed_through_to_close_position(self):
        """closing_reason gets used during the close (for the ledger/log
        record) then cleared once flat again - same as every other close
        in this bot - so this checks it was PASSED THROUGH, not that it
        survives after the fact."""
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}

        captured = {}
        original_close = inst._close_position

        async def spy_close(reason):
            captured["reason"] = reason
            await original_close(reason)
        inst._close_position = spy_close

        await inst.emergency_flatten_and_stop(reason="manual_stop_all")

        self.assertEqual(captured["reason"], "manual_stop_all")


class TestGlobalStopAllResumeAll(unittest.IsolatedAsyncioTestCase):
    """The cross-platform orchestration - confirms it reaches every
    instance on BOTH managers, runs concurrently (not one-at-a-time), and
    a single instance failing doesn't stop the others from being attempted."""

    async def test_stop_all_calls_every_instance_on_both_managers(self):
        import app.global_control as gc

        inst_a = mock.AsyncMock()
        inst_b = mock.AsyncMock()
        inst_c = mock.AsyncMock()

        fake_binance_manager = mock.Mock()
        fake_binance_manager.instances = {"acc1:BTCUSDT": inst_a, "acc1:ETHUSDT": inst_b}
        fake_okx_manager = mock.Mock()
        fake_okx_manager.instances = {"acc2:BTC-USDT-SWAP": inst_c}

        with mock.patch.object(gc, "manager", fake_binance_manager), \
             mock.patch.object(gc, "okx_manager", fake_okx_manager):
            result = await gc.emergency_stop_all()

        inst_a.emergency_flatten_and_stop.assert_called_once()
        inst_b.emergency_flatten_and_stop.assert_called_once()
        inst_c.emergency_flatten_and_stop.assert_called_once()
        self.assertEqual(set(result["stopped"]),
                          {"acc1:BTCUSDT", "acc1:ETHUSDT", "acc2:BTC-USDT-SWAP"})
        self.assertEqual(result["failed"], [])

    async def test_one_instance_failing_does_not_stop_the_others(self):
        import app.global_control as gc

        inst_ok = mock.AsyncMock()
        inst_failing = mock.AsyncMock()
        inst_failing.emergency_flatten_and_stop.side_effect = ConnectionError("network down")

        fake_binance_manager = mock.Mock()
        fake_binance_manager.instances = {"acc1:BTCUSDT": inst_ok, "acc1:ETHUSDT": inst_failing}
        fake_okx_manager = mock.Mock()
        fake_okx_manager.instances = {}

        with mock.patch.object(gc, "manager", fake_binance_manager), \
             mock.patch.object(gc, "okx_manager", fake_okx_manager):
            result = await gc.emergency_stop_all()

        self.assertIn("acc1:BTCUSDT", result["stopped"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["key"], "acc1:ETHUSDT")

    async def test_resume_all_calls_start_all_enabled_on_both_managers(self):
        import app.global_control as gc

        fake_binance_manager = mock.Mock()
        fake_binance_manager.start_all_enabled = mock.AsyncMock()
        fake_binance_manager.instances = {"a": 1, "b": 2}
        fake_okx_manager = mock.Mock()
        fake_okx_manager.start_all_enabled = mock.AsyncMock()
        fake_okx_manager.instances = {"c": 1}

        with mock.patch.object(gc, "manager", fake_binance_manager), \
             mock.patch.object(gc, "okx_manager", fake_okx_manager):
            result = await gc.resume_all()

        fake_binance_manager.start_all_enabled.assert_called_once()
        fake_okx_manager.start_all_enabled.assert_called_once()
        self.assertEqual(result["binance_running"], 2)
        self.assertEqual(result["okx_running"], 1)


if __name__ == "__main__":
    unittest.main()
