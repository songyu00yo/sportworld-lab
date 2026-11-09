"""Offline outdoor-mode regressions; no emulator or actual app input."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import signal
import unittest
from unittest.mock import patch, Mock, MagicMock

from emulator_motion import MotionError
import run_session as session


class OutdoorSessionTests(unittest.TestCase):
    def test_running_activity_is_exact_and_mode_specific(self):
        for mode, name in (("campus", "RunRandomActivity"), ("outdoor", "OutdoorRunActivity")):
            for prefix in (".", session.PACKAGE + "."):
                activity = session.PACKAGE + "/" + prefix + "mvi.run." + name
                self.assertTrue(session.running_activity_matches(activity, mode))
                other_mode = "campus" if mode == "outdoor" else "outdoor"
                self.assertFalse(session.running_activity_matches(activity, other_mode))
                self.assertFalse(session.running_activity_matches(activity + "Extra", mode))
                self.assertFalse(session.running_activity_matches("other.package/" + prefix + "mvi.run." + name, mode))

    def test_credit_bypass_is_outdoor_only_and_pending_still_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            runs = private / 'runs'
            runs.mkdir()
            (private / 'session-status.json').write_text(json.dumps({
                'valid_credit_observed': True,
                'acceptance': {'record_display_date': '2026/11/6'}}))
            record = runs / 'a.json'
            record.write_text(json.dumps({'state': 'accepted', 'date': '2026-11-06',
                                          'result': {'credited': True}}))
            session.check_daily_credit('2026-11-06', private, mode='outdoor')
            with self.assertRaisesRegex(MotionError, '今天已有'):
                session.check_daily_credit('2026-11-06', private)
            for mode in ('campus', 'outdoor'):
                for status in ('armed', 'running', 'paused', 'review_pending', 'failed', 'preparation_stopped'):
                    record.write_text(json.dumps({'state': status}))
                    with self.subTest(mode=mode, status=status), self.assertRaisesRegex(MotionError, '待核验'):
                        session.check_daily_credit('2026-11-07', private, mode=mode)
            with self.assertRaises(MotionError):
                session.check_daily_credit('2026-11-07', private, mode='unknown')

    def result_fixture(self, mode, answers, *, web=False):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            path = runs / ('a' * 32 + '.json')
            state = {'state': 'review_pending', 'pace_target_s_km': 250}
            if mode is not None:
                state['mode'] = mode
            path.write_text(json.dumps(state))
            device = Mock()
            device.activity.return_value = session.PACKAGE + '/.mvi.run.RunHistoryDetailActivity'
            with patch.object(session, 'Device', return_value=device), patch.object(session, 'RUNS', runs), patch.object(session, 'web_active', return_value=web), \
                 patch('builtins.input', side_effect=answers) as prompts, contextlib.redirect_stdout(io.StringIO()):
                session.record_result(path)
            return json.loads(path.read_text()), prompts

    def test_outdoor_saved_result_never_credited_or_checkpoint_prompted(self):
        state, prompts = self.result_fixture('outdoor', ['已保存户外记录', '0.78', '3:30', '4:29'])
        self.assertFalse(state['result']['credited'])
        self.assertEqual(state['state'], 'reviewed')
        self.assertEqual(state['mode'], 'outdoor')
        self.assertEqual(state['result']['distance_km'], .78)
        self.assertNotIn('checkpoints_total', state['result'])
        self.assertEqual(prompts.call_count, 4)

    def test_outdoor_no_record_resolves_pending(self):
        state, prompts = self.result_fixture('outdoor', ['未产生跑步记录'])
        self.assertFalse(state['result']['credited'])
        self.assertEqual(state['state'], 'reviewed')
        self.assertEqual(prompts.call_count, 1)

    def test_legacy_result_defaults_to_campus(self):
        state, _ = self.result_fixture(None, ['已计分', '1.05', '4:23', '4:10', '3', '3'])
        self.assertEqual(state['mode'], 'campus')
        self.assertTrue(state['result']['credited'])
        self.assertEqual(state['result']['checkpoints_total'], 3)

    def test_web_result_is_one_click_without_mandatory_metrics(self):
        state, prompts = self.result_fixture('outdoor', ['已保存户外记录'], web=True)
        self.assertEqual(prompts.call_count, 1)
        self.assertEqual(state['state'], 'reviewed')
        self.assertEqual(state['result']['source'], 'operator_confirmed_app_outcome')
        self.assertFalse(state['result']['credited'])
        self.assertNotIn('distance_km', state['result'])
        self.assertNotIn('pace_s_km', state['result'])

    def test_deferred_result_does_not_invent_an_outcome(self):
        state, prompts = self.result_fixture('outdoor', ['defer'], web=True)
        self.assertEqual(prompts.call_count, 1)
        self.assertEqual(state['state'], 'review_pending')
        self.assertNotIn('result', state)

    def test_only_completed_restored_outdoor_results_are_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            runs = private/'runs'
            runs.mkdir()
            path = runs/'a.json'
            for mode, state, restored, blocked in (
                ('outdoor', 'review_pending', True, False),
                ('outdoor', 'review_pending', False, True),
                ('outdoor', 'failed', True, True),
                ('outdoor', 'running', True, True),
                ('campus', 'review_pending', True, True),
            ):
                path.write_text(json.dumps({'mode':mode,'state':state,'segments':[{'restoration_verified':restored}]}))
                for next_mode in ('campus','outdoor'):
                    with self.subTest(mode=mode, state=state, restored=restored, next_mode=next_mode):
                        if blocked:
                            with self.assertRaisesRegex(MotionError,'待核验'):
                                session.check_daily_credit(private=private, mode=next_mode)
                        else:
                            session.check_daily_credit(private=private, mode=next_mode)


class PreparationStateTests(unittest.TestCase):
    def test_preparation_cancel_candidates_require_no_sync_or_segments(self):
        base = {"state": "preparation_stopped", "segments": [], "result": None}
        self.assertTrue(session.preparation_cancellable(base))
        self.assertTrue(session.preparation_cancellable({**base, "state": "failed", "error": "手动中止"}))
        for change in ({"segments": [{}]}, {"segments": None},
                       {"first_sync_confirmed_at": "confirmed"}, {"state": "armed"},
                       {"state": "failed", "error": "connection failed"},
                       {"result": {"credited": False}}):
            with self.subTest(change=change):
                self.assertFalse(session.preparation_cancellable({**base, **change}))

    def test_cancelled_preparation_does_not_block_either_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            (private / "runs").mkdir()
            (private / "runs" / "cancelled.json").write_text(json.dumps({
                "state": "cancelled_preparation", "segments": [], "result": None}))
            for mode in ("campus", "outdoor"):
                session.check_daily_credit(private=private, mode=mode)

    def test_pre_sync_stop_and_post_sync_failure_are_distinct(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                route = root / "route.json"
                route.write_text("{}")
                device = Mock()
                device.preflight.return_value = {}
                device.activity.return_value = session.PACKAGE + "/.mvi.home.HomeActivity"
                # The first check after confirmation fails before segment creation.
                def fail_after_confirmation():
                    saved = [json.loads(path.read_text()) for path in root.glob("*.json") if path != route]
                    self.assertEqual(saved[0]["phase"], "sync_confirmed")
                    self.assertTrue(saved[0]["first_sync_confirmed_at"])
                    raise MotionError("device changed")
                device.verify_boot.side_effect = fail_after_confirmation
                args = Mock(mode="outdoor", seed=1, distance=1050, pace=250, cadence=175)
                acknowledge = Mock(side_effect=None if confirmed else KeyboardInterrupt())
                with contextlib.ExitStack() as stack:
                    for name, value in (("RunLock", MagicMock()), ("Device", Mock(return_value=device)),
                                        ("Console", Mock()), ("identify", Mock()),
                                        ("values", Mock(return_value=[0, 0, 0])),
                                        ("build_samples", Mock(return_value=([{"longitude": 1, "latitude": 2}],
                                                                           {"distance_m": 1050, "duration_s": 260}))),
                                        ("verify_samples", Mock()), ("StartLocationKeepalive", MagicMock()),
                                        ("acknowledge", acknowledge), ("RUNS", root), ("transmit", Mock())):
                        stack.enter_context(patch.object(session, name, value))
                    stack.enter_context(patch.object(session, "check_daily_credit"))
                    stack.enter_context(patch("ui_protocol.publish_plan"))
                    stack.enter_context(patch("motion_report.expected_timeline", return_value=[]))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    with self.assertRaises(MotionError):
                        session.interactive_run(route, {}, Mock(sha256="fixture"), args)
                    session.transmit.assert_not_called()
                records = [json.loads(path.read_text()) for path in root.glob("*.json") if path != route]
                self.assertEqual(len(records), 1)
                state = records[0]
                self.assertEqual(state["segments"], [])
                self.assertIsNone(state["result"])
                self.assertEqual(state["state"], "failed" if confirmed else "preparation_stopped")
                self.assertEqual(bool(state["first_sync_confirmed_at"]), confirmed)
                self.assertEqual(session.preparation_cancellable(state), not confirmed)

    def test_completed_input_stop_requires_successful_transmission_and_restoration(self):
        cases = (
            ("awaiting_app_pause", True, "review_pending", True),
            ("awaiting_app_distance", False, "review_pending", True),
            ("transmission_interrupted", True, "failed", True),
            ("warmup_interrupted", True, "failed", True),
            ("transmission_interrupted_and_restoration_failed", True, "failed", False),
            ("restoration_failed", True, "failed", False),
        )
        for stop_at, web, expected_state, restored in cases:
            with self.subTest(stop_at=stop_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                route = root / "route.json"
                route.write_text("{}")
                device = Mock()
                device.preflight.return_value = {}
                device.activity.return_value = session.PACKAGE + "/.mvi.home.HomeActivity"
                args = Mock(mode="outdoor", seed=1, distance=1200, pace=200, cadence=175)
                samples = [{"longitude": 1, "latitude": 2, "phase_s": 240}]
                metadata = {"distance_m": 1200, "duration_s": 240}

                # All device, transmission, recovery and UI calls are offline doubles.
                def transmit_offline(samples, connection, monitor, metrics, *, outdoor_start=False):
                    self.assertTrue(outdoor_start)
                    device.activity.return_value = session.PACKAGE + "/.mvi.run.OutdoorRunActivity"
                    metrics["last_point"] = (1, 2)
                    metrics["input_elapsed_s"] = 120 if stop_at.startswith("transmission_interrupted") else 240
                    if stop_at == "warmup_interrupted":
                        metrics["input_elapsed_s"] = 5
                        raise KeyboardInterrupt()
                    if stop_at == "transmission_interrupted_and_restoration_failed":
                        raise MotionError("offline transmission interrupted")
                    if stop_at == "transmission_interrupted":
                        raise KeyboardInterrupt()

                acknowledge = Mock(side_effect=[None, KeyboardInterrupt()] if stop_at == "awaiting_app_pause" else None)
                recover = Mock(side_effect=MotionError("offline restoration failed") if not restored else None)
                with contextlib.ExitStack() as stack:
                    for name, value in (
                        ("RunLock", MagicMock()), ("interruptible", MagicMock()),
                        ("Device", Mock(return_value=device)), ("Console", Mock()),
                        ("identify", Mock()), ("values", Mock(return_value=[0, 0, 0])),
                        ("build_samples", Mock(return_value=(samples, metadata))),
                        ("verify_samples", Mock()), ("StartLocationKeepalive", MagicMock()),
                        ("BootMonitor", MagicMock()), ("gps", Mock()),
                        ("acknowledge", acknowledge), ("RUNS", root),
                        ("transmit", Mock(side_effect=transmit_offline)), ("recover", recover),
                        ("web_active", Mock(return_value=web)),
                        ("numeric_input", Mock(side_effect=KeyboardInterrupt())),
                        ("record_result", Mock()), ("check_daily_credit", Mock()),
                    ):
                        stack.enter_context(patch.object(session, name, value))
                    stack.enter_context(patch.object(session.signal, "signal", return_value=signal.SIG_DFL))
                    stack.enter_context(patch("ui_protocol.publish_plan"))
                    stack.enter_context(patch("ui_protocol.publish_progress"))
                    stack.enter_context(patch("motion_report.expected_timeline", return_value=[]))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    with self.assertRaises(MotionError):
                        session.interactive_run(route, {}, Mock(sha256="offline-fixture"), args)
                    session.transmit.assert_called_once()
                    self.assertTrue(acknowledge.call_args_list[0].kwargs["requires_app_home"])
                    self.assertNotIn("app_run_mode", acknowledge.call_args_list[0].kwargs)
                    session.gps.assert_not_called()
                    from sensor_channels import COUPLED_SENSORS
                    recover.assert_called_once_with(device, (1, 2), {sensor: [0, 0, 0] for sensor in COUPLED_SENSORS})
                    session.record_result.assert_not_called()
                    self.assertEqual(acknowledge.call_count, 2 if stop_at.startswith("awaiting_app_") else 1)
                    self.assertEqual(session.numeric_input.call_count, int(stop_at == "awaiting_app_distance"))
                records = [json.loads(path.read_text()) for path in root.glob("*.json") if path != route]
                self.assertEqual(len(records), 1)
                state = records[0]
                self.assertEqual(state["state"], expected_state)
                self.assertIsNone(state["result"])
                self.assertEqual(len(state["segments"]), 1)
                self.assertIs(state["segments"][0]["restoration_verified"], restored)
                if stop_at == "transmission_interrupted_and_restoration_failed":
                    self.assertEqual(state["error"], "offline transmission interrupted")
                    self.assertEqual(state["segments"][0]["restoration_error"], "offline restoration failed")
                expected_phase = stop_at if stop_at.startswith("awaiting_app_") else "awaiting_app_pause_after_failure"
                self.assertEqual(state["phase"], expected_phase)
                self.assertEqual(bool(state.get("app_session_unresolved")), expected_state == "failed")
                self.assertEqual(state["segments"][0]["input_elapsed_s"],
                                 5 if stop_at == "warmup_interrupted" else
                                 120 if stop_at.startswith("transmission_interrupted") else 240)


class StartLocationKeepaliveTests(unittest.TestCase):
    def test_position_freezes_when_selected_app_run_page_opens(self):
        for mode, activity in (("campus", "RunRandomActivity"), ("outdoor", "OutdoorRunActivity")):
            with self.subTest(mode=mode), patch.object(session, "gps") as gps:
                device, connection = Mock(), Mock()
                home = session.PACKAGE + "/.mvi.home.HomeActivity"
                device.activity.side_effect = [home, home, session.PACKAGE + "/.mvi.run." + activity]
                keepalive = session.StartLocationKeepalive(device, (1, 2), altitude_m=79.2, app_run_mode=mode)
                keepalive.connection = connection
                for _ in range(5):
                    keepalive.update()
                fixes = [call.args[1] for call in gps.call_args_list]
                self.assertNotEqual(fixes[0], fixes[1])
                self.assertEqual(len(fixes), 2)
                self.assertTrue(keepalive.running_seen)
                self.assertEqual(device.activity.call_count, 3)
                for call in gps.call_args_list:
                    self.assertEqual(call.args[2], 0)
                    self.assertEqual(call.kwargs, {"altitude_m": 79.2})

    def test_other_run_mode_does_not_freeze_preparation(self):
        device, connection = Mock(), Mock()
        device.activity.return_value = session.PACKAGE + "/.mvi.run.RunRandomActivity"
        keepalive = session.StartLocationKeepalive(device, (1, 2), app_run_mode="outdoor")
        keepalive.connection = connection
        with patch.object(session, "gps") as gps:
            keepalive.update()
            keepalive.update()
        self.assertFalse(keepalive.running_seen)
        self.assertNotEqual(gps.call_args_list[0].args[1], gps.call_args_list[1].args[1])

    def test_start_update_and_interrupt_cleanup_restore_handler(self):
        device, connection = Mock(), Mock()
        original_handler = signal.getsignal(signal.SIGUSR1)
        with patch.object(session, "Console", return_value=connection), patch.object(session, "identify"), patch.object(session, "gps") as gps:
            keepalive = session.StartLocationKeepalive(device, (1, 2))
            with self.assertRaises(KeyboardInterrupt):
                with keepalive:
                    gps.assert_called_once_with(connection, (1, 2), 0)
                    raise KeyboardInterrupt
            self.assertFalse(keepalive.thread.is_alive())
            connection.close.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGUSR1), original_handler)

    def test_startup_boot_failure_closes_without_worker(self):
        device, connection = Mock(), Mock()
        device.verify_boot.side_effect = MotionError("boot changed")
        with patch.object(session, "Console", return_value=connection), patch.object(session, "identify"), patch.object(session, "gps") as gps:
            keepalive = session.StartLocationKeepalive(device, (1, 2))
            with self.assertRaisesRegex(MotionError, "boot changed"):
                with keepalive:
                    self.fail("must not wait for start")
            self.assertIsNone(keepalive.thread)
            gps.assert_not_called()
            connection.close.assert_called_once()

    def test_one_hz_worker_failure_stops_and_blocks_sender(self):
        device, connection = Mock(), Mock()
        failed = threading.Event()
        times = []
        def update(*args):
            times.append(session.time.monotonic())
            if len(times) == 2:
                raise MotionError("connection lost")
        with patch.object(session, "Console", return_value=connection), patch.object(session, "identify"), patch.object(session, "gps", side_effect=update) as gps, patch.object(session.os, "kill", side_effect=lambda *args: failed.set()) as kill:
            keepalive = session.StartLocationKeepalive(device, (1, 2))
            with self.assertRaisesRegex(MotionError, "保活失败"):
                with keepalive:
                    self.assertTrue(failed.wait(3))
            self.assertFalse(keepalive.thread.is_alive())
            kill.assert_called_once_with(session.os.getpid(), signal.SIGUSR1)
            self.assertGreaterEqual(times[1]-times[0], .9)
            self.assertEqual(device.verify_boot.call_count, 2)
            self.assertEqual(gps.call_args_list[0].args,(connection,(1,2),0))
            for call in gps.call_args_list:
                self.assertIs(call.args[0],connection)
                self.assertEqual(call.args[2],0)
                self.assertLessEqual(session.metres((1,2),call.args[1]),1)
            self.assertNotEqual(gps.call_args_list[0].args[1],gps.call_args_list[1].args[1])
            connection.close.assert_called_once()



if __name__ == '__main__':
    unittest.main()


class TransmissionReliabilityTests(unittest.TestCase):
    """Deterministic offline clocks and transport doubles only."""

    def execute(self, jitter=None, gps_error=False, gps_offset=(0.0, 0.0),
                wake_delay=0.0, gps_latency=0.0):
        class Clock:
            value = 0.0
            sleep_calls = 0

            def __call__(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds
                self.sleep_calls += 1
                if self.sleep_calls == 1:
                    self.value += wake_delay

        clock = Clock()
        calls = 0

        def send(connection, mapping):
            nonlocal calls
            for sensor in mapping:
                calls += 1
                if jitter is not None:
                    clock.value += jitter(calls)

        def send_gps(*args, **kwargs):
            clock.value += gps_latency
            if gps_error:
                raise MotionError("offline GPS failure")

        samples = [{"t_s": i/20, "longitude": i, "latitude": i,
                    "speed_m_s": 4, "distance_m": i/5,
                    "altitude_m": 90+i, "acceleration": (0, 9.8, 0),
                    "gyroscope": (0, 0, 0)} for i in range(601)]
        metrics = {"missed_frames": 0, "gps_samples": 0, "frames": 0,
                   "max_lateness_s": 0, "max_frame_gap_s": 0,
                   "last_point": (8, 9), "last_altitude_m": 77,
                   "gps_offset_degrees": gps_offset}
        gps = Mock(side_effect=send_gps)
        with patch.object(session, "gps", gps), patch.object(session, "set_sensor_values", side_effect=send), \
             patch("ui_protocol.publish_progress") as progress:
            try:
                session.transmit(samples, Mock(), Mock(), metrics, clock, clock.sleep)
                self.gps_calls = gps.call_args_list
                self.telemetry = [call.args[0] for call in progress.call_args_list]
            except MotionError as error:
                return metrics, error
        return metrics, None

    def test_two_warmup_misses_are_retained_and_recover_below_one_percent(self):
        metrics, error = self.execute(jitter=lambda calls: .16 if calls == 80 else 0)
        self.assertIsNone(error)
        self.assertEqual(metrics["missed_frames"], 2)
        self.assertGreaterEqual(metrics["input_elapsed_s"], 30)

    def test_wake_lateness_is_retained_before_overdue_sample_reselection(self):
        metrics, error = self.execute(wake_delay=.16)
        self.assertIsNone(error)
        self.assertEqual(metrics["missed_frames"], 3)
        self.assertAlmostEqual(metrics["max_wake_lateness_s"], .16)
        self.assertAlmostEqual(metrics["max_lateness_s"], .01)
        self.assertAlmostEqual(self.telemetry[-1]["max_wake_lateness_s"], .16)
        self.assertEqual(self.telemetry[-1]["max_gps_batch_s"], 0)
        self.assertEqual(self.telemetry[-1]["max_frame_gap_s"], metrics["max_frame_gap_s"])

    def test_excessive_wake_lateness_keeps_the_original_schedule_on_failure(self):
        metrics, error = self.execute(wake_delay=.251)
        self.assertIn("250 毫秒", str(error))
        self.assertAlmostEqual(metrics["max_wake_lateness_s"], .251)
        frame = metrics["last_frame"]
        self.assertEqual(frame["scheduled_t_s"], .05)
        self.assertAlmostEqual(frame["wake_actual_t_s"], .301)
        self.assertEqual(frame["previous_completed_t_s"], 0)
        self.assertIsNone(frame["selected_sample_t_s"])
        self.assertIsNone(frame["channel_batch_s"])

    def test_sustained_misses_abort_after_twenty_seconds(self):
        metrics, error = self.execute(jitter=lambda calls: .11 if calls % 80 == 0 else 0)
        self.assertIn("漏发比例", str(error))
        self.assertGreaterEqual(metrics["input_elapsed_s"], 20)
        self.assertGreater(metrics["missed_frames"]/(metrics["frames"]+metrics["missed_frames"]), .01)

    def test_quarter_second_safety_applies_during_warmup(self):
        metrics, error = self.execute(jitter=lambda calls: .251 if calls == 2 else 0)
        self.assertIn("250 毫秒", str(error))
        self.assertLess(metrics["input_elapsed_s"], 20)
        frame = metrics["last_frame"]
        self.assertEqual(frame["scheduled_t_s"], 0)
        self.assertEqual(frame["selected_sample_t_s"], 0)
        self.assertAlmostEqual(frame["channel_batch_s"], .251)
        self.assertAlmostEqual(frame["completed_t_s"], .251)

    def test_failed_gps_keeps_its_duration_without_claiming_an_acknowledged_fix(self):
        metrics, error = self.execute(gps_error=True, gps_latency=.15)
        self.assertIn("offline GPS failure", str(error))
        self.assertAlmostEqual(metrics["max_gps_batch_s"], .15)
        frame = metrics["last_frame"]
        self.assertEqual(frame["scheduled_t_s"], 0)
        self.assertEqual(frame["wake_actual_t_s"], 0)
        self.assertEqual(frame["selected_sample_t_s"], 0)
        self.assertIsNone(frame["previous_completed_t_s"])
        self.assertIsNone(frame["channel_batch_s"])
        self.assertAlmostEqual(frame["gps_batch_s"], .15)
        self.assertAlmostEqual(frame["completed_t_s"], .15)
        self.assertEqual(metrics["last_point"], (8, 9))
        self.assertEqual(metrics["last_altitude_m"], 77)
        self.assertEqual(metrics["gps_samples"], 0)
        self.assertEqual(metrics["frames"], 0)

    def test_failed_first_gps_does_not_replace_confirmed_initial_anchor(self):
        metrics, error = self.execute(gps_error=True)
        self.assertIn("offline GPS failure", str(error))
        self.assertEqual(metrics["last_point"], (8, 9))
        self.assertEqual(metrics["last_altitude_m"], 77)
        self.assertEqual(metrics["gps_samples"], 0)


class AppSessionGuardTests(unittest.TestCase):
    def test_idle_helper_rejects_both_running_modes_and_unknown_foreground(self):
        device = Mock()
        for activity in ("", "other.app/.HomeActivity",
                         session.PACKAGE+"/.mvi.run.OutdoorRunActivity",
                         session.PACKAGE+"/.mvi.run.RunRandomActivity"):
            for home in (False, True):
                with self.subTest(activity=activity, home=home):
                    device.activity.return_value = activity
                    with self.assertRaises(MotionError):
                        session.require_app_idle(device, home=home)
        device.activity.return_value = session.PACKAGE+"/.mvi.run.RunHistoryDetailActivity"
        session.require_app_idle(device)
        with self.assertRaises(MotionError):
            session.require_app_idle(device, home=True)
        device.activity.return_value = session.PACKAGE+"/.mvi.home.HomeActivity"
        session.require_app_idle(device, home=True)

    def test_fresh_run_rejects_active_app_before_sensor_capture_or_reservation(self):
        device = Mock()
        device.preflight.return_value = {}
        for mode in ("campus", "outdoor"):
            device.activity.return_value = session.PACKAGE+"/.mvi.run."+(
                "OutdoorRunActivity" if mode == "outdoor" else "RunRandomActivity")
            with patch.object(session, "RunLock", MagicMock()), \
                 patch.object(session, "check_daily_credit"), \
                 patch.object(session, "validate_route_review"), \
                 patch.object(session, "Device", return_value=device), \
                 patch.object(session, "Console") as console, \
                 patch.object(session, "atomic_json") as save:
                with self.assertRaisesRegex(MotionError, "进行中的跑步"):
                    session.interactive_run(Path("offline.json"), {}, Mock(), Mock(mode=mode))
                console.assert_not_called()
                save.assert_not_called()

    def test_result_cannot_clear_active_app_before_prompt_or_after_answer(self):
        home = session.PACKAGE+"/.mvi.home.HomeActivity"
        running = session.PACKAGE+"/.mvi.run.OutdoorRunActivity"
        for activities, prompt_count in (([running], 0), ([home, running], 1)):
            with self.subTest(activities=activities), tempfile.TemporaryDirectory() as directory:
                runs = Path(directory)
                path = runs/("b"*32+".json")
                original = {"state":"failed", "mode":"outdoor", "app_session_unresolved":True}
                path.write_text(json.dumps(original))
                device = Mock()
                device.activity.side_effect = activities
                with patch.object(session, "RUNS", runs), \
                     patch.object(session, "Device", return_value=device), \
                     patch.object(session, "web_active", return_value=True), \
                     patch.object(session, "choose", return_value="未产生跑步记录") as choose, \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(MotionError, "进行中的跑步"):
                        session.record_result(path)
                    self.assertEqual(choose.call_count, prompt_count)
                self.assertEqual(json.loads(path.read_text()), original)


class GpsObservationAnchorTests(unittest.TestCase):
    def test_constant_bias_preserves_first_anchor_and_relative_displacements(self):
        fixture = TransmissionReliabilityTests()
        delta = (.000001, -.000002)
        metrics, error = fixture.execute(gps_offset=delta)
        self.assertIsNone(error)
        self.assertEqual(fixture.gps_calls[0].args[1], delta)
        self.assertEqual(len(fixture.gps_calls), 31)
        for previous, current in zip(fixture.gps_calls, fixture.gps_calls[1:]):
            self.assertAlmostEqual(current.args[1][0]-previous.args[1][0], 20)
            self.assertAlmostEqual(current.args[1][1]-previous.args[1][1], 20)
        self.assertEqual(metrics["last_point"], fixture.gps_calls[-1].args[1])

    def test_fresh_anchor_requires_finite_submetre_offset(self):
        sample = {"longitude":121.5,"latitude":38.8}
        point = (121.500001,38.800002)
        delta = session.gps_anchor_offset(point,sample)
        self.assertAlmostEqual(sample["longitude"]+delta[0], point[0])
        self.assertAlmostEqual(sample["latitude"]+delta[1], point[1])
        for bad in ((121.5001,38.8),(float("nan"),38.8),(121.5,float("inf"))):
            with self.subTest(point=bad), self.assertRaises(MotionError):
                session.gps_anchor_offset(bad,sample)

    def test_channel_preparation_never_publishes_a_fix(self):
        class Connection:
            pass
        connection = Connection()
        with patch.dict(session.os.environ,{"SPORTWORLD_GPS_TRANSPORT":"grpc"}), \
             patch("emulator_gps.CachedGps") as cached:
            session.prepare_gps_channel(connection)
            session.prepare_gps_channel(connection)
            cached.assert_called_once_with(session.AVD,session.PORT)
            cached.return_value.update.assert_not_called()


class OutdoorPrerollTests(unittest.TestCase):
    """Clock and activity doubles exercise cues without any device access."""
    def execute(self, start_at=8.5, *, outdoor=True, duration=275):
        class Clock:
            value = 0.0
            def __call__(self):
                return self.value
            def sleep(self, seconds):
                self.value += seconds
        clock = Clock()
        class Monitor:
            app_start_observed_s = None
            app_start_observed_at = None
            app_start_last_absent_s = 0.0
            def begin_input(self, started):
                self.started = started
            def check(self):
                if start_at is not None and clock.value >= start_at:
                    self.app_start_observed_s = start_at
                    self.app_start_observed_at = "offline-activity-observation"
                    self.app_start_last_absent_s = start_at-.5
        samples = [{"t_s": i/20, "longitude": 121.5+i*.000001, "latitude": 38.8,
                    "speed_m_s": 4, "distance_m": i/5,
                    "acceleration": (0, 9.8, 0), "gyroscope": (0, 0, 0),
                    "magnetic_field": (0, 10, -45), "orientation_deg": (0, -90, 0)}
                   for i in range(duration*20+1)]
        metrics = {"frames": 0, "gps_samples": 0, "missed_frames": 0,
                   "max_lateness_s": 0.0, "max_frame_gap_s": 0.0}
        error = None
        with patch.object(session, "gps") as gps, patch.object(session, "set_sensor_values") as sensors, \
             patch("ui_protocol.publish_progress") as progress, contextlib.redirect_stdout(io.StringIO()):
            try:
                session.transmit(samples, Mock(), Monitor(), metrics, clock, clock.sleep,
                                 outdoor_start=outdoor)
            except MotionError as caught:
                error = caught
        return metrics, [call.args[0] for call in progress.call_args_list], gps, sensors, error

    def test_cues_preserve_all_frames_and_separate_record_window(self):
        metrics, events, gps, sensors, error = self.execute()
        self.assertIsNone(error)
        self.assertEqual(metrics["frames"], 5501)
        self.assertEqual(metrics["missed_frames"], 0)
        self.assertEqual(gps.call_count, 276)
        self.assertEqual(sensors.call_count, 5501)
        self.assertEqual(metrics["input_elapsed_s"], 275)
        self.assertEqual(metrics["sent_distance_m"], 1100)
        self.assertEqual(metrics["app_window_model_distance_m"], 1018)
        self.assertEqual(metrics["app_window_model_duration_s"], 254.5)
        self.assertEqual(metrics["requested_pause_elapsed_s"], 263)
        self.assertIsNone(metrics["app_pause_observed_s"])
        self.assertEqual(events[0]["operator_action"], "warmup")
        self.assertEqual(events[0]["warmup_remaining_s"], 8)
        self.assertEqual(events[8]["operator_action"], "start_app")
        self.assertIsNone(events[9]["operator_action"])
        self.assertTrue(all(event["operator_action"] == "pause_app" for event in events[263:]))
        self.assertTrue(all(event["phase"] == "sending" for event in events))
        self.assertTrue(all(len(call.args[1]) == 7 for call in sensors.call_args_list))

    def test_delayed_start_preserves_observation_source_and_resolution(self):
        metrics, events, _, _, error = self.execute(start_at=18)
        self.assertIsNone(error)
        self.assertEqual(metrics["app_start_observed_s"], 18)
        self.assertEqual(metrics["app_start_observed_at"], "offline-activity-observation")
        self.assertEqual(metrics["app_start_observation_resolution_s"], .5)
        self.assertEqual(metrics["app_window_model_duration_s"], 245)
        self.assertEqual(metrics["app_window_model_distance_m"], 980)
        self.assertEqual(events[17]["operator_action"], "start_app")
        self.assertIsNone(events[18]["operator_action"])

    def test_missing_start_stops_at_twenty_seconds(self):
        metrics, events, _, _, error = self.execute(start_at=None)
        self.assertIn("20 秒", str(error))
        self.assertEqual(metrics["frames"], 400)
        self.assertLess(metrics["input_elapsed_s"], 20)
        self.assertNotIn("app_start_observed_s", metrics)
        self.assertTrue(all(event["operator_action"] != "pause_app" for event in events))

    def test_early_start_is_recorded_with_warning(self):
        metrics, _, _, _, error = self.execute(start_at=3)
        self.assertIsNone(error)
        self.assertTrue(metrics["app_start_observed_early"])
        self.assertEqual(metrics["app_start_observed_s"], 3)

    def test_default_sender_does_not_add_warmup_or_require_app_start(self):
        metrics, events, _, _, error = self.execute(start_at=None, outdoor=False, duration=2)
        self.assertIsNone(error)
        self.assertEqual(metrics["frames"], 41)
        self.assertNotIn("warmup_s", metrics)
        self.assertTrue(all(event["operator_action"] is None for event in events))

    def test_background_monitor_latches_activity_and_absent_bound(self):
        device = Mock()
        device.activity.side_effect = [session.PACKAGE+"/.mvi.home.HomeActivity",
                                       session.PACKAGE+"/.mvi.run.OutdoorRunActivity"]
        monitor = session.BootMonitor(device, outdoor_start=True)
        monitor.begin_input(1000)
        monitor.stop_event = Mock()
        monitor.stop_event.wait.side_effect = [False, False, True]
        with patch.object(session.time, "monotonic", side_effect=[1006, 1008.5]):
            monitor.watch()
        self.assertEqual(monitor.app_start_last_absent_s, 6)
        self.assertEqual(monitor.app_start_observed_s, 8.5)
        self.assertEqual(device.activity.call_count, 2)

    def test_default_boot_monitor_does_not_read_app_activity(self):
        device = Mock()
        monitor = session.BootMonitor(device)
        monitor.stop_event = Mock()
        monitor.stop_event.wait.side_effect = [False, True]
        monitor.watch()
        device.activity.assert_not_called()
