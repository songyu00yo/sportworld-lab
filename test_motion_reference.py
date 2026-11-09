"""Offline cadence regressions using labelled mathematical test signals."""
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import motion_reference as motion
from emulator_motion import MotionError


def gait_signal(cadence, hz, seconds=30):
    """Two foot contacts per stride, with unequal alternating contacts."""
    step_hz = cadence/60
    return [math.sin(2*math.pi*step_hz*i/hz)+.65*math.cos(math.pi*step_hz*i/hz)
            for i in range(round(seconds*hz))]


class CadenceEstimationTests(unittest.TestCase):
    def test_alternating_contacts_estimate_steps_and_stride_separately(self):
        for hz in (20, 50):
            for cadence in (145, 175, 200):
                with self.subTest(hz=hz, cadence=cadence):
                    result = motion.estimate_period(gait_signal(cadence, hz), hz)
                    self.assertEqual(result["confidence"], "high")
                    self.assertAlmostEqual(result["cadence_estimate"], cadence, delta=2)
                    self.assertEqual(result["period_s"], result["step_period_s"])
                    self.assertAlmostEqual(result["stride_period_s"], 2*result["period_s"])
                    self.assertEqual(result["autocorrelation_period"], "stride_period_s")

    def test_dominant_higher_harmonic_does_not_relabel_step_cadence(self):
        hz, cadence = 50, 162
        values = gait_signal(cadence, hz)
        values = [v+3*math.sin(4*math.pi*cadence/60*i/hz) for i, v in enumerate(values)]
        result = motion.estimate_period(values, hz)
        self.assertEqual(result["confidence"], "high")
        self.assertAlmostEqual(result["cadence_estimate"], cadence, delta=1)

    def test_inconsistent_windows_and_short_inputs_have_low_confidence(self):
        values = sum((gait_signal(cadence, 50, 10) for cadence in (145, 175, 200)), [])
        result = motion.estimate_period(values, 50)
        self.assertEqual(result["confidence"], "low")
        self.assertGreater(result["max_window_relative_deviation"], .08)
        self.assertEqual(motion.estimate_period(gait_signal(175, 50, 5), 50)["confidence"], "low")

    def test_stationary_inputs_do_not_invent_steps(self):
        for values in ([9.81]*1500, []):
            result = motion.estimate_period(values, 50)
            self.assertEqual(result["confidence"], "low")
            self.assertIsNone(result["cadence_estimate"])
            self.assertIsNone(result["period_s"])
        with self.assertRaises(MotionError):
            motion.estimate_period([float("nan")]*1500, 50)

    def test_source_frame_preserves_up_despite_quaternion_sign_changes(self):
        poses = [(1, 0, 0, 0), (-1, 0, 0, 0)]*100
        bases, metadata = motion.source_frame(poses)
        self.assertEqual(len(bases), len(poses))
        self.assertEqual(metadata["source_up_axis"], (0, 0, 1))
        self.assertEqual(metadata["source_up_axis_deviation_deg"]["max"], 0)
        with self.assertRaises(MotionError):
            motion.source_frame([(float("nan"), 0, 0, 0)]*100)

    def test_source_up_resampling_matches_original_filter_and_trim_timeline(self):
        bases = [motion.axis_angle((1, 0, 0), i*.002) for i in range(100)]
        axes = [motion.rotate(motion.conjugate(base), (0, 0, 1)) for base in bases]
        weights = (1, 4, 6, 4, 1)
        filtered = [tuple(sum(weights[k]*axes[i+k][j] for k in range(5))/16
                          for j in range(3)) for i in range(96)]
        sampled = motion.source_up_samples(bases)
        self.assertEqual(len(sampled), int(95*20/50))
        expected = [motion.unit(filtered[0]),
                    motion.unit(tuple((a+b)/2 for a, b in zip(filtered[2], filtered[3])))]
        for actual, target in zip(sampled[:2], expected):
            for a, b in zip(actual, target):
                self.assertAlmostEqual(a, b)


@unittest.skipUnless((motion.REFERENCE_DIR/"A_DeviceMotion_data.zip").exists() and
                     (motion.REFERENCE_DIR/"template.json").exists(),
                     "Pinned public source is not cached")
class CachedSourceCalibrationTests(unittest.TestCase):
    def test_legacy_cache_is_recalibrated_without_rewriting_its_bytes(self):
        rows = motion.read_record(motion.TRAIN_MEMBER)
        legacy = json.loads((motion.REFERENCE_DIR/"template.json").read_text())
        legacy["cadence"] = {"period_s": .3, "cadence_estimate": 200,
                              "autocorrelation": .37}
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory)/"template.json"
            template.write_text(json.dumps(legacy))
            before = hashlib.sha256(template.read_bytes()).hexdigest()
            (Path(directory)/"source.json").write_text(json.dumps({"template_sha256": before}))
            with patch.object(motion, "REFERENCE_DIR", Path(directory)), \
                    patch.object(motion, "read_record", return_value=rows):
                reference = motion.Reference(template)
            self.assertEqual(before, hashlib.sha256(template.read_bytes()).hexdigest())
        calibration = reference.metadata["cadence"]
        self.assertEqual(calibration["method"], motion.CADENCE_METHOD)
        self.assertEqual(calibration["confidence"], "high")
        self.assertGreater(reference.cadence, 155)
        self.assertLess(reference.cadence, 175)
        self.assertEqual(reference.metadata["cached_cadence"]["cadence_estimate"], 200)
        self.assertTrue(reference.metadata["cadence_cache_recalibrated"])
        self.assertEqual(reference.cadence_legacy, 200)
        self.assertEqual(reference.metadata["cadence_legacy"], 200)
        self.assertAlmostEqual(motion.norm(reference.source_up_axis), 1)
        self.assertTrue(all(math.isfinite(v) for v in reference.source_up_axis))
        self.assertIn("checksum-verified", reference.metadata["source_up_axis_source"])
        deviation = reference.metadata["source_up_axis_deviation_deg"]
        self.assertTrue(all(math.isfinite(v) for v in deviation.values()))
        self.assertGreater(deviation["p95"], 40)
        self.assertGreater(deviation["max"], deviation["p95"])

    def test_rebuild_and_runtime_use_same_source_calibration(self):
        rows = motion.read_record(motion.TRAIN_MEMBER)
        expected = motion.source_cadence(rows)
        runtime = motion.Reference(motion.REFERENCE_DIR/"template.json")
        cached_samples = json.loads((motion.REFERENCE_DIR/"template.json").read_text())["samples"]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(motion, "REFERENCE_DIR", Path(directory)), \
                    patch.object(motion, "download_reference") as download, \
                    patch.object(motion, "read_record", return_value=rows):
                prepared, manifest = motion.prepare_template()
                self.assertEqual(json.loads(prepared.read_text())["samples"], cached_samples)
            download.assert_called_once_with()
        self.assertEqual(manifest["cadence"], expected)
        self.assertEqual(manifest["source_up_axis"], runtime.source_up_axis)
        self.assertEqual(manifest["source_up_axis_deviation_deg"], runtime.metadata["source_up_axis_deviation_deg"])

    def test_aligned_knots_preserve_norms_and_legacy_sample_values(self):
        reference = motion.Reference(motion.REFERENCE_DIR/"template.json")
        times = (0, 10, 20, reference.period)
        before = [reference.sample(t) for t in times]
        target = (0, 1, 0)
        pose, acceleration = reference.sample_aligned(20, target)
        original_pose, original_acceleration = reference.sample(20)
        self.assertAlmostEqual(motion.norm(acceleration), motion.norm(original_acceleration))
        self.assertAlmostEqual(motion.norm(pose), 1)
        rotation = motion.align_vectors(reference.source_up_axes[400], target)
        expected = motion.qmul(motion.qmul(rotation, original_pose), motion.conjugate(rotation))
        self.assertAlmostEqual(abs(sum(a*b for a, b in zip(pose, expected))), 1)
        reference.sample_aligned(21, (0, 2, 0))
        self.assertEqual(len(reference._aligned_cache), 1)
        self.assertEqual(before, [reference.sample(t) for t in times])
        residual = reference.metadata["source_alignment"]["targets"][0]
        self.assertLess(residual["max_knot_up_alignment_error_deg"], 1e-4)
        with self.assertRaises(MotionError):
            reference.sample_aligned(20, (0, float("nan"), 1))

    def test_alignment_precedes_interpolation_and_keeps_seams_continuous(self):
        reference = motion.Reference(motion.REFERENCE_DIR/"template.json")
        target = (0, 0, 1)
        aligned_knots = []
        for index in (400, 401):
            rotation = motion.align_vectors(reference.source_up_axes[index], target)
            aligned_knots.append(motion.rotate(rotation, reference.samples[index]["a"]))
        _, actual = reference.sample_aligned(20.025, target)
        for value, left, right in zip(actual, *aligned_knots):
            self.assertAlmostEqual(value, (left+right)/2)
        for time_s in (0, reference.overlap, reference.period, 2*reference.period):
            left = reference.sample_aligned(time_s-1e-6, target)
            right = reference.sample_aligned(time_s+1e-6, target)
            self.assertLess(motion.norm(tuple(a-b for a, b in zip(left[1], right[1]))), .001)
            self.assertAlmostEqual(abs(sum(a*b for a, b in zip(left[0], right[0]))), 1, places=7)

    def test_missing_verified_source_cannot_fall_back_to_legacy_label(self):
        with patch.object(motion, "read_record", side_effect=MotionError("verified source missing")):
            with self.assertRaisesRegex(MotionError, "verified source missing"):
                motion.Reference(motion.REFERENCE_DIR/"template.json")


if __name__ == "__main__":
    unittest.main()
