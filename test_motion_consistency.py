"""Offline regressions for stride, waveform cadence and GPS height continuity."""
import hashlib
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from emulator_motion import MotionError
from motion_reference import unit
import run_session as session
from run_trajectory import build_samples, filter_gait_acceleration, verify_samples
from test_coupled_motion import FakeReference, route_fixture


class AlignedReference(FakeReference):
    """Known mathematical signal, used only to test independent cadence checks."""
    true_cadence = 175

    def sample_aligned(self, time_s, target_up_axis):
        amplitude = 1.5*math.sin(time_s*self.true_cadence/60*2*math.pi)
        return (1, 0, 0, 0), tuple(amplitude*value for value in unit(target_up_axis))


class MotionConsistencyTests(unittest.TestCase):
    def test_stride_stays_bounded_during_start_and_stop(self):
        samples, metadata = build_samples(route_fixture(), FakeReference())
        self.assertLessEqual(max(sample["step_length_m"] for sample in samples), 2.1)
        self.assertEqual(samples[0]["step_length_m"], 0)
        self.assertEqual(samples[-1]["step_length_m"], 0)
        for sample in samples:
            self.assertAlmostEqual(sample["step_length_m"]*sample["cadence_nominal_spm"]/60,
                                   sample["speed_m_s"])
            if sample["speed_m_s"] < .3:
                self.assertLess(sample["step_length_m"], .35)
        verify_samples(samples, metadata)

    def test_incompatible_low_cadence_is_rejected_before_export(self):
        with self.assertRaisesRegex(MotionError, "单步距离"):
            build_samples(route_fixture(), FakeReference(), cadence=120)

    def test_waveform_check_rejects_a_falsely_labeled_reference(self):
        reference = AlignedReference()
        samples, metadata = build_samples(route_fixture(), reference)
        self.assertTrue(verify_samples(samples, metadata)["waveform_cadence_windows"])
        reference.true_cadence = 120
        samples, metadata = build_samples(route_fixture(), reference)
        with self.assertRaisesRegex(MotionError, "动作波形"):
            verify_samples(samples, metadata)

    def test_gps_observation_error_does_not_change_imu_or_speed(self):
        samples, metadata = build_samples(route_fixture(), FakeReference(), seed=9)
        elevation = {**metadata["elevation_profile"], "max_error_m": 0}
        flat, _ = build_samples(route_fixture(), FakeReference(), seed=9, elevation=elevation)
        self.assertGreater(max(sample["altitude_m"] for sample in samples)-min(sample["altitude_m"] for sample in samples), 1)
        for left, right in zip(samples, flat):
            for key in ("position_m", "speed_m_s", "pose", "acceleration", "gyroscope", "cadence_nominal_spm"):
                self.assertEqual(left[key], right[key])
            self.assertEqual(right["altitude_m"], right["ground_altitude_m"])

    def test_continuation_preserves_altitude_and_step_phase(self):
        samples, metadata = build_samples(route_fixture(), FakeReference(), seed=9)
        last = samples[-1]
        continued, _ = build_samples(route_fixture(), FakeReference(), 50, offset=1050,
                                      phase=last["phase_s"], seed=9, allow_slower=True,
                                      elapsed_offset_s=last["absolute_time_s"], elevation=metadata["elevation_profile"],
                                      magnetic_field_world=metadata["magnetic_field_world"])
        for key in ("altitude_m", "ground_altitude_m", "absolute_time_s", "step_count_model", "phase_s", "position_m", "magnetic_field"):
            self.assertEqual(last[key], continued[0][key])

    def test_v3_filters_gait_and_preserves_other_motion_channels(self):
        historical, old_metadata = build_samples(route_fixture(), FakeReference(), model_version=2, seed=9)
        samples, metadata = build_samples(route_fixture(), FakeReference(), model_version=3, seed=9)
        self.assertEqual(metadata["motion_model_version"], 3)
        self.assertNotIn("gait_antialias_filter", old_metadata)
        self.assertEqual(metadata["gait_antialias_filter"]["cutoff_hz"], 4)
        self.assertEqual(metadata["gait_antialias_filter"]["taps"], 41)
        self.assertEqual(metadata["gait_antialias_filter"]["dc_gain"], 1)
        self.assertEqual(metadata["gait_antialias_filter"]["phase"], "zero")
        self.assertEqual(metadata["gait_antialias_filter"]["scope"], "world gait acceleration only")
        self.assertIn("gait_antialias", metadata["impact_model"])
        changed = {"gait_acceleration_world", "acceleration", "linear_acceleration"}
        for original, filtered in zip(historical, samples):
            for key in original.keys()-changed:
                self.assertEqual(original[key], filtered[key], key)
        self.assertNotEqual(samples[800]["gait_acceleration_world"], historical[800]["gait_acceleration_world"])
        for endpoint in (samples[0], samples[-1]):
            self.assertEqual(endpoint["gait_acceleration_world"], (0, 0, 0))
        self.assertTrue(verify_samples(samples, metadata)["passed"])
        continued, continuation_metadata = build_samples(
            route_fixture(), FakeReference(), distance=50, offset=1050,
            phase=samples[-1]["phase_s"], seed=9, allow_slower=True, model_version=3,
            elapsed_offset_s=samples[-1]["absolute_time_s"],
            elevation=metadata["elevation_profile"], magnetic_field_world=metadata["magnetic_field_world"])
        self.assertEqual(continuation_metadata["gait_antialias_filter"], metadata["gait_antialias_filter"])
        self.assertEqual(continued[0]["phase_s"], samples[-1]["phase_s"])
        self.assertTrue(verify_samples(continued, continuation_metadata)["passed"])

    def test_v2_sensor_output_matches_captured_pre_filter_baseline(self):
        samples, metadata = build_samples(route_fixture(), FakeReference(), model_version=2, seed=9)
        keys = ("gait_acceleration_world", "acceleration", "gyroscope", "pose", "gravity_body",
                "magnetic_field", "linear_acceleration", "orientation_deg")
        serialized = json.dumps([{key: sample[key] for key in keys} for sample in samples],
                                sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(),
                         "a22b91a3f3a1e744391b7762bab4ed8eb826b9c51f07e76fff8fcd9adca523b4")
        self.assertTrue(verify_samples(samples, metadata)["passed"])

    def test_gait_filter_has_unit_dc_gain_zero_phase_and_rejects_high_frequency(self):
        # Mathematical fixtures test bandwidth only, without asserting App step counts.
        samples = [{"speed_m_s": 3, "gait_acceleration_world":
                    (2, math.sin(2*math.pi*3*i/20), math.sin(2*math.pi*8*i/20))}
                   for i in range(200)]
        filter_gait_acceleration(samples)
        for index in range(30, 170):
            gait = samples[index]["gait_acceleration_world"]
            self.assertAlmostEqual(gait[0], 2)
            self.assertLess(abs(gait[1]-math.sin(2*math.pi*3*index/20)), .02)
            self.assertLess(abs(gait[2]), .01)

    def test_legacy_plans_keep_original_time_scaling_and_height(self):
        old_reference = FakeReference()
        corrected_reference = FakeReference()
        corrected_reference.cadence_legacy = old_reference.cadence
        corrected_reference.cadence = 160
        corrected_reference.sample = old_reference.sample
        old, _ = build_samples(route_fixture(), old_reference, model_version=1)
        rebuilt, _ = build_samples(route_fixture(), corrected_reference, model_version=1)
        for left, right in zip(old, rebuilt):
            for key in ("phase_s", "acceleration", "gyroscope", "cadence_nominal_spm", "altitude_m"):
                self.assertEqual(left[key], right[key])
            self.assertEqual(right["altitude_m"], 50)


class HeightTransportTests(unittest.TestCase):
    def test_sender_drives_all_coupled_channels(self):
        from sensor_channels import COUPLED_SENSORS
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        samples = [{"t_s": i*.05, "longitude": 121.5, "latitude": 38.8, "speed_m_s": 0,
                    "distance_m": 0, "heading_rad": 0, "acceleration": (0, 9.8, 0),
                    "gyroscope": (0, .1, 0), "magnetic_field": (0, 10, -45),
                    "orientation_deg": (0, -90, 0)} for i in range(3)]
        metrics = {"frames": 0, "gps_samples": 0, "missed_frames": 0,
                   "max_lateness_s": 0, "max_frame_gap_s": 0}
        with patch.object(session, "gps"), patch.object(session, "set_sensor_values") as setter:
            session.transmit(samples, Mock(), Mock(), metrics, clock=lambda: now[0], sleep=sleep)
        names = [list(call.args[1]) for call in setter.call_args_list]
        self.assertEqual(names, [list(COUPLED_SENSORS)]*3)
        self.assertEqual(metrics["sensor_channels_sent"], list(COUPLED_SENSORS))
        first = setter.call_args_list[0].args[1]
        self.assertEqual(first["acceleration"], first["acceleration-uncalibrated"])
        self.assertEqual(first["gyroscope"], first["gyroscope-uncalibrated"])

    def test_sender_and_recovery_retain_last_observation_height(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        samples = [{"t_s": i*.05, "longitude": 121.5, "latitude": 38.8, "speed_m_s": 1,
                    "distance_m": i*.05, "arc_m": i*.05, "heading_rad": 0,
                    "acceleration": (0, 9.8, 0), "gyroscope": (0, 0, 0),
                    "cadence_nominal_spm": 120, "altitude_m": 79+i*.1} for i in range(3)]
        metrics = {"frames": 0, "gps_samples": 0, "missed_frames": 0,
                   "max_lateness_s": 0, "max_frame_gap_s": 0}
        with patch.object(session, "gps") as gps, patch.object(session, "set_sensor_values"):
            session.transmit(samples, Mock(), Mock(), metrics, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(metrics["last_altitude_m"], 79.2)
        self.assertEqual([call.kwargs["altitude_m"] for call in gps.call_args_list], [79, 79.2])
        with patch.object(session, "Console"), patch.object(session, "identify"), \
             patch.object(session, "restore"), patch.object(session, "gps") as gps:
            session.recover(Mock(), (121.5, 38.8), {}, last_altitude_m=metrics["last_altitude_m"])
        self.assertEqual(gps.call_args.kwargs["altitude_m"], 79.2)
        self.assertEqual(gps.call_args.args[2], 0)

    def test_stationary_preparation_keeps_same_height(self):
        keepalive = session.StartLocationKeepalive(Mock(), (121.5, 38.8), altitude_m=79.4)
        keepalive.connection = Mock()
        with patch.object(session, "gps") as gps:
            keepalive.update()
            keepalive.update()
        for call in gps.call_args_list:
            self.assertEqual(call.kwargs["altitude_m"], 79.4)
            self.assertEqual(call.args[2], 0)


if __name__ == "__main__":
    unittest.main()
