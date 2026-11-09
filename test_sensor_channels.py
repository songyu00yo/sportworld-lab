"""Offline sensor-contract regressions; all console I/O uses mocks."""
import contextlib
import io
import math
import unittest
from unittest.mock import Mock, call, patch

import emulator_motion as motion
import sensor_channels as channels


class SensorChannelTests(unittest.TestCase):
    def test_capture_reads_all_required_channels_in_order(self):
        for coupled, expected in ((True, channels.COUPLED_SENSORS), (False, motion.SENSORS)):
            with self.subTest(coupled=coupled), patch.object(channels, "values", return_value=(1, 2, 3)) as read:
                console = Mock()
                baseline = channels.capture_baseline(console, coupled=coupled)
                self.assertEqual(tuple(baseline), expected)
                self.assertEqual(read.call_args_list, [call(console, sensor) for sensor in expected])

    def test_sample_mapping_preserves_legacy_and_couples_uncalibrated_inputs(self):
        sample = {"acceleration": (1, 2, 3), "gyroscope": (4, 5, 6)}
        self.assertEqual(channels.sample_sensor_inputs(sample), sample)
        sample["magnetic_field"] = (7, 8, 9)
        self.assertEqual(tuple(channels.sample_sensor_inputs(sample)), motion.SENSORS)
        sample["orientation_deg"] = (10, 11, 12)
        inputs = channels.sample_sensor_inputs(sample)
        self.assertEqual(tuple(inputs), channels.COUPLED_SENSORS)
        for calibrated, uncalibrated in (
            ("acceleration", "acceleration-uncalibrated"),
            ("gyroscope", "gyroscope-uncalibrated"),
            ("magnetic-field", "magnetic-field-uncalibrated"),
        ):
            self.assertEqual(inputs[calibrated], inputs[uncalibrated])
        self.assertEqual(inputs["orientation"], sample["orientation_deg"])

    def test_android_orientation_known_rotations_use_degrees_and_android_axis_order(self):
        root = math.sqrt(.5)
        cases = (
            ((0, 0, 9.8), (0, 20, -40), (0, 0, 0)),
            ((0, 0, 9.8), (-20, 0, -40), (90, 0, 0)),
            ((0, 0, 9.8), (20, 0, -40), (270, 0, 0)),
            ((0, -9.8*root, 9.8*root), (0, root, root), (0, 45, 0)),
            ((-9.8*root, 0, 9.8*root), (0, 20, 0), (0, 0, 45)),
        )
        for gravity, magnetic, expected in cases:
            with self.subTest(expected=expected):
                actual = channels.android_orientation(gravity, magnetic)
                for value, target in zip(actual, expected):
                    self.assertAlmostEqual(value, target)

    def test_singular_pitch_keeps_finite_android_convention(self):
        for sign in (-1, 1):
            actual = channels.android_orientation((0, sign*9.8, 0), (0, 0, 20))
            self.assertTrue(all(math.isfinite(value) for value in actual))
            self.assertAlmostEqual(actual[1], -sign*90)

    def test_degenerate_and_nonfinite_orientation_vectors_are_rejected(self):
        for gravity, magnetic in (
            ((0, 0, 0), (0, 20, 0)),
            ((0, 0, 9.8), (0, 0, 20)),
            ((0, 0, 9.8), (0, 0, 0)),
            ((0, 0, 9.8), (0, .00001, 20)),
            ((0, 0, math.nan), (0, 20, 0)),
            ((0, 0, 9.8), (0, math.inf, 0)),
            ((0, 9.8), (0, 20, 0)),
        ):
            with self.subTest(gravity=gravity, magnetic=magnetic), self.assertRaises(motion.MotionError):
                channels.android_orientation(gravity, magnetic)


class MultiChannelRestoreTests(unittest.TestCase):
    def test_restore_writes_and_verifies_every_saved_channel(self):
        for sensors in (motion.SENSORS, channels.COUPLED_SENSORS):
            original = {sensor: (index, index+1, index+2) for index, sensor in enumerate(sensors)}
            connection = Mock()
            connection.command.return_value = "offline-avd"
            identity_check = Mock()
            with self.subTest(sensors=sensors), patch.object(motion, "Console", return_value=connection), \
                 patch.object(motion, "set_values") as write, \
                 patch.object(motion, "values", side_effect=lambda console, sensor: original[sensor]) as read, \
                 contextlib.redirect_stdout(io.StringIO()):
                motion.restore(15556, "offline-avd", original, identity_check)
                self.assertEqual(write.call_args_list, [call(connection, sensor, original[sensor]) for sensor in sensors])
                self.assertEqual(read.call_args_list, [call(connection, sensor) for sensor in sensors])
                self.assertEqual(identity_check.call_count, 1+2*len(sensors))
                connection.close.assert_called_once()

    def test_invalid_baselines_are_rejected_before_opening_console(self):
        base = {sensor: (0, 0, 0) for sensor in motion.SENSORS}
        for original in ({"acceleration": (0, 0, 0)}, {**base, "unknown": (0, 0, 0)},
                         {**base, "orientation": (0, math.nan, 0)},
                         {**base, "gyroscope": (0, 0)}):
            with self.subTest(original=original), patch.object(motion, "Console") as console:
                with self.assertRaises(motion.MotionError):
                    motion.restore(15556, "offline-avd", original)
                console.assert_not_called()

    def test_write_failure_still_attempts_remaining_saved_channels(self):
        original = {sensor: (0, 0, 0) for sensor in channels.COUPLED_SENSORS}
        connection = Mock()
        connection.command.return_value = "offline-avd"
        failures = [motion.MotionError("offline rejection")] + [None]*6
        with patch.object(motion, "Console", return_value=connection), \
             patch.object(motion, "set_values", side_effect=failures) as write:
            with self.assertRaisesRegex(motion.MotionError, "RESTORE FAILED"):
                motion.restore(15556, "offline-avd", original)
            self.assertEqual(write.call_count, len(original))
            connection.close.assert_called_once()

    def test_additional_channel_readback_mismatch_fails_verification(self):
        original = {sensor: (0, 0, 0) for sensor in channels.COUPLED_SENSORS}
        connection = Mock()
        connection.command.return_value = "offline-avd"
        for mismatched in channels.COUPLED_SENSORS:
            with self.subTest(sensor=mismatched), patch.object(motion, "Console", return_value=connection), \
                 patch.object(motion, "set_values"), \
                 patch.object(motion, "values", side_effect=lambda console, sensor: (1, 0, 0) if sensor == mismatched else original[sensor]):
                with self.assertRaisesRegex(motion.MotionError, "RESTORE VERIFICATION FAILED"):
                    motion.restore(15556, "offline-avd", original)


if __name__ == "__main__":
    unittest.main()
