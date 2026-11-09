"""Offline altitude regressions. No device connection or App operation is made."""
import copy
import json
import math
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

from elevation_profile import ElevationProfile, DEFAULT_BASE_M, _reference_hash
from emulator_motion import MotionError


class ElevationProfileTests(unittest.TestCase):
    def setUp(self):
        self.reference_path = Path(__file__).resolve().parent / 'examples/elevation-reference.json'
        patcher = mock.patch('elevation_profile.USER_REFERENCE_PATH', self.reference_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_seed_repeatability_and_distinct_starting_observations(self):
        first, repeat, changed = ElevationProfile(23), ElevationProfile(23), ElevationProfile(24)
        self.assertEqual(first.metadata, repeat.metadata)
        for t in range(301):
            self.assertEqual(first.at(t, t * 5), repeat.at(t, t * 5))
        self.assertNotEqual(first.at(0, 0), changed.at(0, 0))
        for seed in range(30):
            self.assertTrue(0 <= ElevationProfile(seed).metadata['source_time_offset_s'] <= 15)

    def test_flat_ground_and_bounded_reference_observations(self):
        for seed in range(20):
            profile = ElevationProfile(seed)
            for tick in range(6001):
                t = tick * .05
                sample = profile.at(t, t * 5)
                self.assertEqual(sample['ground_altitude_m'], DEFAULT_BASE_M)
                self.assertLessEqual(abs(sample['altitude_m'] - DEFAULT_BASE_M), 3.3 + 1e-12)
            self.assertEqual(profile.at(10, 50), profile.at(10, 450))

    def test_synthetic_reference_retains_timestamps_and_declares_its_origin(self):
        profile = ElevationProfile()
        metadata = copy.deepcopy(profile.metadata)
        metadata['source_time_offset_s'] = 0
        profile = ElevationProfile.from_metadata(metadata)
        reference = metadata['reference_trace']
        self.assertEqual(reference['original_duration_s'], 600)
        self.assertEqual(reference['displayed_range_m'], [75.9, 82.5])
        self.assertFalse(reference['digitized_approximation'])
        self.assertEqual(reference['source_kind'], 'synthetic')
        self.assertEqual(metadata['source_kind'], 'synthetic')
        self.assertNotIn('screenshot_sha256', reference)
        self.assertIn('Synthetic', metadata['source'])
        self.assertIn('Synthetic', metadata['height_reference'])
        self.assertIn('synthetic', metadata['noise_model'])
        self.assertEqual(reference['vertical_datum'], 'unverified')
        self.assertEqual(metadata['time_scale'], 1)
        for time_s, height_m in reference['samples']:
            self.assertAlmostEqual(profile.at(time_s, 0)['altitude_m'], height_m, places=12)
        self.assertEqual(profile.at(600, 0), profile.at(1200, 0))
        self.assertNotEqual(profile.at(600, 0), profile.at(0, 0))

    def test_offset_uses_source_seconds_without_duration_compression(self):
        original = ElevationProfile(8)
        metadata = copy.deepcopy(original.metadata)
        offset = metadata['source_time_offset_s']
        metadata['source_time_offset_s'] = 0
        unshifted = ElevationProfile.from_metadata(metadata)
        for t in (0, 19.5, 120, 299.95, 300):
            self.assertEqual(original.at(t, 0), unshifted.at(t + offset, 0))

    def test_absolute_time_continuation_uses_self_contained_metadata(self):
        uninterrupted = ElevationProfile(8)
        saved = json.loads(json.dumps(uninterrupted.metadata))
        with mock.patch('elevation_profile.USER_REFERENCE_PATH', Path('/missing/reference.json')):
            resumed = ElevationProfile.from_metadata(saved)
        for offset in (0, .05, 1, 17, 180, 500):
            self.assertEqual(uninterrupted.at(120 + offset, 600 + offset * 5),
                             resumed.at(120 + offset, 600 + offset * 5))
        saved['reference_trace']['samples'][0][1] = 80
        self.assertEqual(uninterrupted.metadata, resumed.metadata)

    def test_custom_base_range_and_source_remain_supported(self):
        standard = ElevationProfile(8)
        custom = ElevationProfile(8, base_m=100, max_error_m=2,
                                  source='Test-only flat assumption')
        for t in range(301):
            expected = 100 + (standard.at(t, 0)['altitude_m'] - DEFAULT_BASE_M) * 2 / 3.3
            self.assertAlmostEqual(custom.at(t, 0)['altitude_m'], expected)
            self.assertEqual(custom.at(t, 0)['ground_altitude_m'], 100)
        self.assertEqual(custom.metadata['source'], 'Test-only flat assumption')
        self.assertIn('unverified', custom.metadata['height_reference'])
        self.assertIn('not a surveyed', standard.metadata['source'])
        self.assertNotEqual(ElevationProfile(base_m=80).metadata['source'], standard.metadata['source'])
        self.assertEqual(ElevationProfile(max_error_m=0).at(123, 456)['altitude_m'], DEFAULT_BASE_M)
        self.assertLessEqual(abs(ElevationProfile(max_error_m=20).at(20, 0)['altitude_m'] - 79.2), 20)

    def test_historical_v1_wave_metadata_roundtrip_exactly(self):
        rng = random.Random('elevation-v1:23')
        waves = [(0.65, rng.uniform(35, 65), rng.uniform(0, 2 * math.pi)),
                 (0.35, rng.uniform(95, 160), rng.uniform(0, 2 * math.pi))]
        metadata = {
            'model_version': 1, 'ground_model': 'flat assumption; no terrain survey',
            'base_m': 79.2, 'source': 'Historical test-only assumption',
            'height_reference': 'User App displayed altitude; vertical datum unverified',
            'seed': 23, 'max_error_m': 3.3,
            'noise_model': 'bounded smooth temporal GPS observation error', 'waves': waves,
        }
        serialized = json.loads(json.dumps(metadata))
        with mock.patch('elevation_profile.USER_REFERENCE_PATH', Path('/missing/reference.json')):
            profile = ElevationProfile.from_metadata(serialized)
        self.assertEqual(profile.metadata, serialized)
        for t in (0, .05, 40, 120, 300, 1000):
            expected = 79.2 + 3.3 * sum(
                weight * math.sin(2 * math.pi * t / period + phase)
                for weight, period, phase in waves
            )
            self.assertEqual(profile.at(t, t * 5)['altitude_m'], expected)

    def test_explicit_and_legacy_digitized_sources_keep_historical_replay(self):
        # Fabricated digest and trace exercise the schema without using a person's data.
        reference = json.loads(self.reference_path.read_text())
        reference.update(source_kind='digitized_app_chart', digitized_approximation=True,
                         screenshot_sha256='a' * 64,
                         source='Test fixture for historical chart metadata, not an actual screenshot')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference.json'
            for explicit in (True, False):
                if not explicit:
                    reference.pop('source_kind')
                path.write_text(json.dumps(reference))
                profile = ElevationProfile(reference_path=path)
                self.assertEqual(profile.metadata['source_kind'], 'digitized_app_chart')
                self.assertIn('User App', profile.metadata['height_reference'])
                metadata = copy.deepcopy(profile.metadata)
                if not explicit:
                    metadata.pop('source_kind')
                restored = ElevationProfile.from_metadata(metadata)
                self.assertEqual(restored.metadata, metadata)
                for time_s in (0, 40, 300, 600, 900):
                    self.assertEqual(restored.at(time_s, 0), profile.at(time_s, 0))

    def test_false_or_inconsistent_reference_provenance_is_rejected(self):
        reference = json.loads(self.reference_path.read_text())
        mutations = (
            {'digitized_approximation': True}, {'screenshot_sha256': 'a' * 64},
            {'screenshot_sha256': None}, {'source_kind': 'unknown'}, {'source_kind': None},
            {'source_kind': 'digitized_app_chart'},
            {'source_kind': 'digitized_app_chart', 'digitized_approximation': True},
            {'source_kind': 'digitized_app_chart', 'digitized_approximation': True,
             'screenshot_sha256': 'invalid'},
        )
        for mutation in mutations:
            metadata = copy.deepcopy(ElevationProfile().metadata)
            metadata['reference_trace'] = dict(reference, **mutation)
            metadata['reference_sha256'] = _reference_hash(metadata['reference_trace'])
            with self.subTest(mutation=mutation), self.assertRaises(MotionError):
                ElevationProfile.from_metadata(metadata)
        metadata = copy.deepcopy(ElevationProfile().metadata)
        metadata['source_kind'] = 'digitized_app_chart'
        with self.assertRaisesRegex(MotionError, 'source kind does not match'):
            ElevationProfile.from_metadata(metadata)

    def test_missing_or_malformed_public_reference_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference.json'
            with mock.patch('elevation_profile.USER_REFERENCE_PATH', path):
                with self.assertRaisesRegex(MotionError, 'Cannot read elevation reference'):
                    ElevationProfile()
                for content in ('{', '{}', '[]'):
                    path.write_text(content)
                    with self.assertRaises(MotionError):
                        ElevationProfile()
                reference = json.loads(self.reference_path.read_text())
                reference['samples'][1][0] = 0
                path.write_text(json.dumps(reference))
                with self.assertRaisesRegex(MotionError, 'ordered'):
                    ElevationProfile()

    def test_invalid_metadata_is_rejected(self):
        good = ElevationProfile().metadata
        invalid = [None, [], {}, {'model_version': 3}]
        mutations = (
            ('model_version', 3), ('model_version', True), ('seed', True),
            ('base_m', math.nan), ('max_error_m', 21), ('source', ''),
            ('source_time_offset_s', -1), ('source_time_offset_s', 16),
            ('source_time_offset_s', math.nan), ('time_scale', .5), ('time_scale', True),
            ('out_of_range_policy', 'repeat'), ('reference_sha256', 'invalid'),
            ('reference_trace', None),
        )
        for key, value in mutations:
            bad = copy.deepcopy(good)
            bad[key] = value
            invalid.append(bad)
        for bad in invalid:
            with self.subTest(metadata=bad), self.assertRaises(MotionError):
                ElevationProfile.from_metadata(bad)
        for waves in (None, [], [[1, 0, 0]], [[True, 40, 0]],
                      [[1.1, 40, 0]], [[1, 40, math.inf]]):
            bad = dict(good, model_version=1, waves=waves)
            with self.subTest(waves=waves), self.assertRaises(MotionError):
                ElevationProfile.from_metadata(bad)

    def test_invalid_parameters_and_queries(self):
        for options in ({'seed': True}, {'seed': 1.2}, {'base_m': math.nan},
                        {'base_m': math.inf}, {'base_m': -501}, {'max_error_m': -1},
                        {'max_error_m': 21}, {'source': ''}):
            with self.subTest(options=options), self.assertRaises(MotionError):
                ElevationProfile(**options)
        profile = ElevationProfile()
        for t, arc in ((-1, 0), (0, -1), (math.nan, 0), (0, math.inf), (True, 0)):
            with self.subTest(t=t, arc=arc), self.assertRaises(MotionError):
                profile.at(t, arc)


if __name__ == '__main__':
    unittest.main()
