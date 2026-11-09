"""Offline regressions. All synthetic fixtures and device doubles stay in tests."""
import contextlib
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import emulator_route
import motion_reference as reference
import motion_report
import run_session as session
import run_trajectory as trajectory
from emulator_motion import MotionError


def route_fixture():
    x, y = emulator_route.STADIUM_SEED
    return {"closed": True, "points": [[x, y], [x+.0007, y], [x+.0007, y+.0005], [x, y+.0005]]}


class FakeReference:
    """Mathematical fixture, never written into the production reference cache."""
    sha256 = "offline-test-fixture"
    cadence = 175.0
    period = 10.0

    def sample(self, time_s):
        phase = time_s*2*math.pi*self.cadence/60
        return reference.axis_angle((1, 0, 0), .05*math.sin(phase)), (.1*math.sin(phase), 1.5*math.sin(phase), .2*math.cos(phase))


class GeometryAndPhysicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = route_fixture()
        cls.samples, cls.metadata = trajectory.build_samples(cls.data, FakeReference(), distance=1050)

    def test_core_motion_attitude_matches_gravity_convention(self):
        roll, pitch, yaw = -.8, -1.2, .7
        q = reference.attitude_quaternion(roll, pitch, yaw)
        expected = (math.sin(roll)*math.cos(pitch), -math.sin(pitch), -math.cos(roll)*math.cos(pitch))
        actual = reference.rotate(reference.conjugate(q), (0, 0, -1))
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b)
        android_static = tuple(-reference.G*v for v in (0, 0, -1))
        self.assertEqual(android_static, (0, 0, reference.G))

    def test_quaternion_gyro_integrates_into_observed_pose(self):
        left = reference.axis_angle((1, 2, 3), .8)
        right = reference.qmul(left, reference.axis_angle((2, -1, 1), .04))
        gyro = reference.angular_velocity(left, right, .02)
        reconstructed = reference.qmul(left, reference.axis_angle(gyro, reference.norm(gyro)*.02))
        self.assertAlmostEqual(abs(sum(a*b for a, b in zip(reconstructed, right))), 1)

    def test_alignment_supports_opposite_gravity(self):
        q = reference.align_vectors((0, 0, -1), (0, 0, 1))
        self.assertAlmostEqual(reference.rotate(q, (0, 0, -1))[2], 1)

    def test_coupled_1050_m_plan_meets_target_and_bounds(self):
        checks = trajectory.verify_samples(self.samples, self.metadata)
        self.assertTrue(checks["passed"])
        self.assertAlmostEqual(self.samples[-1]["t_s"], 262.5)
        self.assertAlmostEqual(self.samples[-1]["distance_m"], 1050)
        self.assertEqual(self.samples[0]["speed_m_s"], 0)
        self.assertEqual(self.samples[-1]["speed_m_s"], 0)
        self.assertLess(checks["position_speed_max_m_s"], .15)

    def test_route_hits_original_vertices_and_closes_smoothly(self):
        path = trajectory.SmoothPath(self.data)
        for vertex in self.data["points"]:
            distance = min(emulator_route.metres(tuple(vertex), path.coordinates(point)) for point in path.points)
            self.assertLess(distance, .01)
        before = path.at(path.length-1e-4)
        after = path.at(path.length+1e-4)
        self.assertLess(reference.norm(tuple(a-b for a, b in zip(before[0], after[0]))), .001)
        self.assertLess(abs(before[1]-after[1]), .001)

    def test_turns_have_lateral_acceleration_and_speed_reduction(self):
        bend = max(self.samples, key=lambda s: abs(s["curvature"])*s["speed_m_s"]**2)
        self.assertGreater(reference.norm(bend["route_acceleration_world"]), .1)
        self.assertLess(bend["speed_m_s"], max(s["speed_m_s"] for s in self.samples))

    def test_continuation_preserves_position_and_template_phase(self):
        last = self.samples[-1]
        samples, metadata = trajectory.build_samples(self.data, FakeReference(), distance=50, offset=1050,
                                                     phase=last["phase_s"], allow_slower=True)
        self.assertAlmostEqual(samples[0]["longitude"], last["longitude"])
        self.assertAlmostEqual(samples[0]["latitude"], last["latitude"])
        self.assertAlmostEqual(samples[0]["phase_s"], last["phase_s"])
        self.assertAlmostEqual(abs(sum(a*b for a, b in zip(last["pose"], samples[0]["pose"]))), 1)
        self.assertLess(262.5+metadata["duration_s"], 300)
        self.assertTrue(trajectory.verify_samples(samples, metadata)["passed"])

    def test_impossible_pace_requires_explicit_slower_continuation(self):
        with self.assertRaises(MotionError):
            trajectory.build_samples(self.data, FakeReference(), distance=5)
        samples, metadata = trajectory.build_samples(self.data, FakeReference(), distance=5, allow_slower=True)
        self.assertGreater(metadata["planned_pace_s_km"], 250)
        trajectory.verify_samples(samples, metadata)

    def test_budgets_nonfinite_and_malformed_inputs_rejected(self):
        for distance, pace in ((1201, 250), (1050, 600), (float("nan"), 250), (-1, 250)):
            with self.subTest(distance=distance, pace=pace), self.assertRaises(MotionError):
                trajectory.build_samples(self.data, FakeReference(), distance=distance, pace=pace)

    def test_corrupted_force_or_gyro_fails_independent_reconstruction(self):
        for key in ("acceleration", "gyroscope"):
            samples = [dict(s) for s in self.samples]
            samples[20][key] = (30, 0, 0)
            with self.subTest(channel=key), self.assertRaises(MotionError):
                trajectory.verify_samples(samples, self.metadata)

    def test_export_default_never_connects_to_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory)/"route.json"
            route_path.write_text(json.dumps(self.data))
            with patch.object(emulator_route, "Console") as console, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(emulator_route.main(["--route", str(route_path), "--check"]), 0)
            console.assert_not_called()


class StateAndRecoveryTests(unittest.TestCase):
    def test_duplicate_lock_refused_and_release_survives_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.lock"
            with session.RunLock(path):
                with self.assertRaises(MotionError):
                    with session.RunLock(path):
                        self.fail("second lock acquired")
            with session.RunLock(path):
                pass

    def test_daily_credit_and_unresolved_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            old = private/"session-status.json"
            old.write_text(json.dumps({"valid_credit_observed": True, "acceptance": {"record_display_date": "2026/11/6"}}))
            with self.assertRaisesRegex(MotionError, "今天已有"):
                session.check_daily_credit("2026-11-06", private)
            session.check_daily_credit("2026-11-07", private)
            (private/"runs").mkdir()
            (private/"runs/a.json").write_text(json.dumps({"state": "running", "result": None, "date": "2026-11-06"}))
            with self.assertRaisesRegex(MotionError, "待核验"):
                session.check_daily_credit("2026-11-07", private)

    def test_review_requires_today_hash_and_checkpoint_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            data = route_fixture()
            data["checkpoint_markers"] = {"mandatory": data["points"][0], "east": data["points"][1], "south": data["points"][2]}
            path = Path(directory)/"route.json"
            path.write_text(json.dumps(data))
            review = {"date": "2026-11-07", "route_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "confirmed_course": True, "method": "user_checked_app_map", "mandatory_key": "mandatory",
                      "checkpoint_keys": list(data["checkpoint_markers"])}
            session.atomic_json(session.review_path(path), review)
            session.validate_route_review(path, data, "2026-11-07")
            with self.assertRaises(MotionError):
                session.validate_route_review(path, data, "2026-11-08")
            path.write_text(path.read_text()+" ")
            with self.assertRaises(MotionError):
                session.validate_route_review(path, data, "2026-11-07")

    def test_different_boot_never_receives_cleanup_writes(self):
        class Rebooted:
            def verify_boot(self):
                raise MotionError("changed boot")
        with patch.object(session, "Console") as console, patch.object(session, "restore") as restore:
            with self.assertRaises(MotionError):
                session.recover(Rebooted(), (1, 2), {})
            console.assert_not_called()
            restore.assert_not_called()

    def test_all_point_agent_review_is_bound_to_its_evidence_file(self):
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory)/'private'; private.mkdir()
            data=route_fixture()
            data['checkpoint_markers']={'old_mandatory':data['points'][0], 'east':data['points'][1], 'south':data['points'][2]}
            route=Path(directory)/'route.json'; route.write_text(json.dumps(data))
            evidence=private/'offline-fixture.png'; evidence.write_bytes(b'Explicit offline evidence-file double')
            review={'date':'2026-11-07','route_sha256':hashlib.sha256(route.read_bytes()).hexdigest(),
                    'confirmed_course':True,'method':'agent_checked_app_map','mandatory_key':None,
                    'review_scope':'all_reference_checkpoint_positions','checkpoint_keys':list(data['checkpoint_markers']),
                    'evidence_file':str(evidence),'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
            session.atomic_json(session.review_path(route),review)
            with patch.object(session,'PRIVATE',private):
                result=session.validate_route_review(route,data,'2026-11-07')
                self.assertEqual(result['app_checkpoint_acceptance'],'unverified')
                evidence.write_bytes(b'Changed offline file double')
                with self.assertRaisesRegex(MotionError,'证据'):
                    session.validate_route_review(route,data,'2026-11-07')

    def test_gps_or_close_failure_still_restores_sensors(self):
        class Device:
            def verify_boot(self):
                pass
        class Connection:
            def __init__(self, port):
                pass
            def command(self, command):
                if command == "avd name":
                    return emulator_route.AVD
                raise MotionError("injected write failure")
            def close(self):
                raise OSError("injected close failure")
        with patch.object(session, "Console", Connection), patch.object(session, "restore") as restore:
            with self.assertRaises(MotionError):
                session.recover(Device(), (1, 2), {})
            restore.assert_called_once()
            self.assertIn("identity_check", restore.call_args.kwargs)

    def test_export_failure_keeps_no_partial_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"samples.jsonl"
            with patch.object(session, "atomic_json", side_effect=OSError("injected disk failure")):
                with self.assertRaises(OSError):
                    motion_report.export_samples(path, [{"t": 0}], {"offline": True})
            self.assertFalse(path.exists())

    def test_existing_report_prevents_new_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"samples.jsonl"
            path.with_suffix(".jsonl.report.json").write_text("preserve")
            with self.assertRaises(FileExistsError):
                motion_report.export_samples(path, [{"t": 0}], {})
            self.assertFalse(path.exists())

    def test_clock_input_accepts_minutes_and_rejects_invalid_values(self):
        self.assertEqual(session.parse_time("4:10"), 250)
        self.assertEqual(session.parse_time("4：23"), 263)
        self.assertEqual(session.parse_time("250"), 250)
        for value in ("4:60", "nan", "inf", "-1", "0", "1:2:3"):
            with self.subTest(value=value), self.assertRaises((MotionError, ValueError)):
                session.parse_time(value)


class TransmissionTests(unittest.TestCase):
    def execute(self, latency=0.001, monitor_error=False):
        class Clock:
            value = 0.0
            def __call__(self):
                return self.value
            def sleep(self, seconds):
                self.value += seconds
        clock = Clock()
        writes = []
        class Console:
            def command(self, command):
                writes.append(command)
                clock.value += latency
                return ""
            def commands(self, commands):
                for command in commands:
                    self.command(command)
        class Monitor:
            def check(self):
                if monitor_error:
                    raise MotionError("boot changed")
        samples = [{"t_s": i/20, "longitude": 121.5, "latitude": 38.8,
                    "speed_m_s": 4, "heading_rad": 0, "distance_m": i/5, "acceleration": (0, 9.8, 0),
                    "gyroscope": (0, 0, 0)} for i in range(101)]
        metrics = {"missed_frames": 0, "gps_samples": 0, "frames": 0,
                   "max_lateness_s": 0, "max_frame_gap_s": 0}
        session.transmit(samples, Console(), Monitor(), metrics, clock, clock.sleep)
        return metrics, writes

    def test_clock_drives_gps_and_imu_without_drift(self):
        metrics, writes = self.execute()
        self.assertEqual(metrics["frames"], 101)
        self.assertEqual(metrics["gps_samples"], 6)
        self.assertEqual(metrics["missed_frames"], 0)
        self.assertLess(metrics["max_frame_gap_s"], .06)
        self.assertAlmostEqual(metrics["observed_imu_hz"], 20, delta=.02)
        rmc = next(command for command in writes if command.startswith("geo nmea $GPRMC,"))
        self.assertEqual(float(rmc.split(",")[8]), 90.0)

    def test_slow_transport_stops_instead_of_accumulating_delayed_samples(self):
        with self.assertRaises(MotionError):
            self.execute(latency=.15)

    def test_boot_monitor_failure_stops_before_next_write(self):
        with self.assertRaisesRegex(MotionError, "boot changed"):
            self.execute(monitor_error=True)


class GuidedFlowTests(unittest.TestCase):
    """Exercise normal entry points with explicit device and transport doubles."""
    def run_flow(self, interrupt=False, outdoor=False, web=False, app_started=True,
                 leave_home_on_ack=False, after_input_page=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        private = Path(temporary.name)
        route_path = private/"route.json"
        data = route_fixture()
        data["checkpoint_markers"] = {"mandatory": data["points"][0], "east": data["points"][1], "south": data["points"][2]}
        route_path.write_text(json.dumps(data))
        review = {"date": session.now().date().isoformat(), "route_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
                  "confirmed_course": True, "method": "user_checked_app_map", "mandatory_key": "mandatory",
                  "checkpoint_keys": list(data["checkpoint_markers"])}
        if not outdoor:
            session.atomic_json(session.review_path(route_path), review)
        writes = []
        app_page = ["home"]
        class Device:
            def preflight(self):
                return {"boot_id": "offline-fixture-boot"}
            def verify_boot(self):
                pass
            def activity(self):
                if app_page[0] == "history":
                    return session.PACKAGE+"/.mvi.run.RunHistoryDetailActivity"
                if app_page[0] == "ad":
                    return session.PACKAGE+"/.advertisement.AdActivity"
                if app_page[0] == "foreign":
                    return "other.package/.HomeActivity"
                if app_page[0] == "unknown":
                    return ""
                if app_page[0] == "home" or not app_started:
                    return session.PACKAGE+"/.mvi.home.HomeActivity"
                return session.PACKAGE+"/.mvi.run."+("OutdoorRunActivity" if outdoor else "RunRandomActivity")
        class Console:
            def __init__(self, port):
                pass
            def command(self, command):
                writes.append(command)
                return emulator_route.AVD if command == "avd name" else ""
            def commands(self, commands):
                for command in commands:
                    self.command(command)
            def close(self):
                pass
        class Monitor:
            def __init__(self, device, *, outdoor_start=False):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        def transport(samples, connection, monitor, metrics, *, outdoor_start=False):
            self.assertEqual(outdoor_start, outdoor)
            if outdoor_start:
                if not app_started:
                    raise MotionError("offline App start deadline")
                app_page[0] = "running"
            metrics["last_point"] = (samples[-1]["longitude"], samples[-1]["latitude"])
            if interrupt:
                raise KeyboardInterrupt
            metrics.update(frames=len(samples), input_elapsed_s=samples[-1]["t_s"], sent_distance_m=samples[-1]["distance_m"])
            if after_input_page is not None:
                app_page[0] = after_input_page
        answers = ["", "", "0.99", "50", "", "", "1.04", "已计分", "1.04", "4:30", "4:20", "3", "3"]
        if outdoor:
            answers = ["", ""] if web else ["", "", "0.78", "已保存户外记录", "0.78", "3:30", "4:29"]
        answer_iterator = iter(answers)
        def answer(prompt):
            if "开始预跑" in prompt:
                self.assertTrue(outdoor)
                self.assertEqual(app_page[0], "home")
                if leave_home_on_ack:
                    app_page[0] = "running"
            elif "倒计时结束" in prompt or "点击 GO" in prompt:
                if outdoor:
                    self.assertIn("出现后立即", prompt)
                    self.assertNotIn("倒计时结束", prompt)
                else:
                    self.assertIn("倒计时结束", prompt)
                self.assertTrue(any(command.startswith("geo nmea $GPGGA,") for command in writes),
                                "stationary GPS must be available before App synchronization")
                app_page[0] = "running"
            response = next(answer_iterator)
            if "App 当前显示距离" in prompt and (outdoor or float(response) >= 1):
                app_page[0] = "history"
            return response
        baseline = {"acceleration": trajectory.DEFAULT_GRAVITY, "gyroscope": (0, 0, 0)}
        baseline.update({"acceleration-uncalibrated": trajectory.DEFAULT_GRAVITY,
                         "gyroscope-uncalibrated": (0, 0, 0), "orientation": (0, 0, 0),
                         "magnetic-field": trajectory.DEFAULT_MAGNETIC_FIELD,
                         "magnetic-field-uncalibrated": trajectory.DEFAULT_MAGNETIC_FIELD})
        args = SimpleNamespace(distance=1050, pace=250, cadence=175, mode="outdoor" if outdoor else "campus")
        with patch.object(session, "PRIVATE", private), patch.object(session, "RUNS", private/"runs"), \
             patch.object(session, "Device", Device), patch.object(session, "Console", Console), \
             patch.object(session, "BootMonitor", Monitor), patch.object(session, "transmit", side_effect=transport), \
             patch.object(session, "values", side_effect=lambda connection, sensor: baseline[sensor]), \
             patch.object(session, "recover") as recover, patch("builtins.input", side_effect=answer), \
             patch.object(session, "web_active", return_value=web), \
             contextlib.redirect_stdout(io.StringIO()):
            if interrupt or not app_started or leave_home_on_ack:
                with self.assertRaises(MotionError):
                    session.interactive_run(route_path, data, FakeReference(), args)
            else:
                self.assertEqual(session.interactive_run(route_path, data, FakeReference(), args), 0)
            recover_count = recover.call_count
        records = list((private/"runs").glob("*.json"))
        self.assertEqual(len(records), 1)
        return json.loads(records[0].read_text()), recover_count

    def test_manually_coordinated_run_continuation_and_result_record(self):
        state, recovery_count = self.run_flow()
        self.assertEqual(state["state"], "accepted")
        self.assertEqual(state["result"]["source"], "operator_entered_from_app")
        self.assertEqual(state["result"]["pace_target_delta_s_km"], 10)
        self.assertEqual(len(state["segments"]), 2)
        self.assertEqual(state["input_distance_m"], 1100)
        self.assertLessEqual(state["input_duration_s"], 300)
        self.assertEqual(state["segments"][1]["planned"]["offset_m"], 1050)
        self.assertEqual(recovery_count, 2)

    def test_outdoor_finishes_below_one_km_without_checkpoint_review(self):
        state, recovery_count = self.run_flow(outdoor=True)
        self.assertEqual(state["mode"], "outdoor")
        self.assertEqual(state["state"], "reviewed")
        self.assertIsNone(state["route_review"])
        self.assertFalse(state["result"]["credited"])
        self.assertEqual(len(state["segments"]), 1)
        self.assertEqual(recovery_count, 1)

    def test_interrupt_restores_and_leaves_a_pending_record(self):
        state, recovery_count = self.run_flow(interrupt=True)
        self.assertEqual(state["state"], "failed")
        self.assertTrue(state["segments"][0]["restoration_verified"])
        self.assertIsNone(state["result"])
        self.assertEqual(recovery_count, 1)

    def test_web_outdoor_returns_after_pause_without_result_questionnaire(self):
        state, recovery_count = self.run_flow(outdoor=True, web=True)
        self.assertEqual(state['state'], 'review_pending')
        self.assertIsNone(state['result'])
        self.assertTrue(session.optional_outdoor_result(state))
        self.assertNotIn('app_distance_km_at_pause', state['segments'][0])
        self.assertEqual(recovery_count, 1)

    def test_web_outdoor_start_deadline_preserves_failed_input_evidence(self):
        state, recovery_count = self.run_flow(outdoor=True, web=True, app_started=False)
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(len(state['segments']), 1)
        self.assertTrue(state['segments'][0]['restoration_verified'])
        self.assertIsNone(state['result'])
        self.assertEqual(recovery_count, 1)

    def test_outdoor_home_is_rechecked_after_initial_acknowledgment(self):
        state, recovery_count = self.run_flow(outdoor=True, web=True, leave_home_on_ack=True)
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["segments"], [])
        self.assertEqual(recovery_count, 0)

    def test_completed_outdoor_result_or_home_skips_redundant_pause_acknowledgment(self):
        for page in ("history", "home"):
            with self.subTest(page=page):
                state, recovery_count = self.run_flow(outdoor=True, web=True, after_input_page=page)
                metrics = state["segments"][0]
                self.assertEqual(state["state"], "review_pending")
                self.assertIsNone(state["result"])
                self.assertIn("app_idle_observed_after_input_at", metrics)
                self.assertNotIn("app_pause_operator_confirmed_at", metrics)
                self.assertNotIn("end_to_pause_ack_delay_s", metrics)
                self.assertEqual(recovery_count, 1)

    def test_running_advertisement_and_unknown_pages_keep_operator_acknowledgment(self):
        for page in ("running", "ad", "foreign", "unknown"):
            with self.subTest(page=page):
                state, _ = self.run_flow(outdoor=True, web=True, after_input_page=page)
                metrics = state["segments"][0]
                self.assertEqual(state["state"], "review_pending")
                self.assertIsNone(state["result"])
                self.assertIn("app_pause_operator_confirmed_at", metrics)
                self.assertIn("end_to_pause_ack_delay_s", metrics)
                self.assertNotIn("app_idle_observed_after_input_at", metrics)


@unittest.skipUnless((reference.REFERENCE_DIR/"template.json").exists(), "Pinned public reference not downloaded")
class PublicReferenceIntegrationTests(unittest.TestCase):
    def test_template_seam_preserves_pose_and_force_continuity(self):
        ref = reference.Reference(reference.REFERENCE_DIR/"template.json")
        for time_s in (0, ref.overlap, ref.period, 2*ref.period):
            left = ref.sample(time_s-1e-6)
            right = ref.sample(time_s+1e-6)
            self.assertLess(reference.norm(tuple(a-b for a, b in zip(left[1], right[1]))), .001)
            self.assertAlmostEqual(abs(sum(a*b for a, b in zip(left[0], right[0]))), 1, places=7)

    def test_real_reference_provenance_and_heldout_identity(self):
        ref = reference.Reference(reference.REFERENCE_DIR/"template.json")
        self.assertEqual(ref.metadata["source_commit"], reference.COMMIT)
        self.assertNotEqual(reference.TRAIN_MEMBER.split("/")[-1], reference.HELDOUT_MEMBER.split("/")[-1])
        self.assertLess(ref.metadata["gravity_max_error_g"], .02)

    def test_modified_template_fails_manifest_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"template.json"
            path.write_bytes((reference.REFERENCE_DIR/"template.json").read_bytes()+b" ")
            with self.assertRaisesRegex(MotionError, "manifest"):
                reference.Reference(path)


if __name__ == "__main__":
    unittest.main()
