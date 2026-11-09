"""Offline seeded pace regressions; mathematical reference is a test fixture."""
import bisect
import hashlib
import json
import math
import random
import statistics
import unittest

from emulator_motion import MotionError
from run_trajectory import TimeLaw, build_path, build_samples, verify_samples
from test_coupled_motion import FakeReference
from test_stadium_track import stadium_fixture


def speed_at_distance(law, distance):
    upper = bisect.bisect_right(law.distances, distance)
    lower = upper-1
    ratio = (distance-law.distances[lower])/(law.distances[upper]-law.distances[lower])
    # Constant acceleration gives speed squared linear in arc length.
    return math.sqrt(law.speeds[lower]**2+ratio*(law.speeds[upper]**2-law.speeds[lower]**2))


class PaceVariationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = stadium_fixture()
        cls.path = build_path(cls.data)

    def test_seed_repeats_profile_and_changes_generated_channels(self):
        first, metadata = build_samples(self.data, FakeReference(), seed=23)
        repeated, repeated_metadata = build_samples(self.data, FakeReference(), seed=23)
        changed, _ = build_samples(self.data, FakeReference(), seed=24)
        self.assertEqual(first, repeated)
        self.assertEqual(metadata, repeated_metadata)
        self.assertEqual(metadata['pace_profile']['seed'], 23)
        for channel in ('speed_m_s', 'cadence_nominal_spm', 'phase_s', 'acceleration', 'gyroscope'):
            self.assertNotEqual(first[800][channel], changed[800][channel], channel)
        verify_samples(first, metadata)

    def test_many_seeds_have_fast_early_eased_middle_and_finishing_push(self):
        for seed in range(40):
            law = TimeLaw(self.path, 1050, 250, seed=seed, profile_version=1)
            def average(start, end):
                return statistics.mean(speed_at_distance(law, s) for s in range(start, end))
            with self.subTest(seed=seed):
                self.assertGreater(average(40, 170), average(450, 800)+.35)
                self.assertGreater(average(960, 1020), average(450, 800)+.8)
                self.assertEqual(law.speeds[0], 0)
                self.assertEqual(law.speeds[-1], 0)
                self.assertAlmostEqual(law.times[-1], 262.5)
                integrated = sum((b-a)*(x+y)/2 for a, b, x, y in
                                 zip(law.times, law.times[1:], law.speeds, law.speeds[1:]))
                self.assertAlmostEqual(integrated, 1050, places=7)
                self.assertTrue(all(b > a for a, b in zip(law.distances, law.distances[1:])))
                self.assertLessEqual(max(law.speeds), 6)
                self.assertLessEqual(max(abs(y*y-x*x)/(2*(b-a)) for a, b, x, y in
                                         zip(law.distances, law.distances[1:], law.speeds, law.speeds[1:])), 1.200001)

    def test_new_profile_varies_locally_throughout_the_run(self):
        for seed in range(40):
            law = TimeLaw(self.path, 1100, 250, seed=seed)
            paces = [10000 / (law.at(t + 10)[0] - law.at(t)[0]) for t in range(10, 260, 10)]
            with self.subTest(seed=seed):
                self.assertEqual(law.profile['version'], 2)
                self.assertAlmostEqual(law.times[-1], 275)
                self.assertEqual(law.distances[-1], 1100)
                # Six native-sized buckets cover a minute. Require visible
                # variation in every moving window, including the middle.
                for index in range(len(paces) - 5):
                    window = paces[index:index + 6]
                    self.assertGreater(max(window) - min(window), 30)
                turns = sum((b-a)*(c-b) < 0 for a, b, c in zip(paces, paces[1:], paces[2:]))
                self.assertGreaterEqual(turns, 8)
                self.assertLessEqual(max(law.speeds), 6)
                self.assertLessEqual(max(abs(y*y-x*x)/(2*(b-a)) for a, b, x, y in
                                         zip(law.distances, law.distances[1:], law.speeds, law.speeds[1:])), 1.200001)

    def test_historical_models_preserve_complete_sample_and_metadata_output(self):
        # Public v1 wave fixture avoids loading any local altitude trace. Each
        # complete output was compared sample by sample and metadata by metadata
        # with the original implementation in an isolated directory before these
        # golden hashes were recorded. Model v1 ignores the elevation argument.
        rng = random.Random('elevation-v1:23')
        elevation = {
            'model_version': 1, 'ground_model': 'flat assumption; no terrain survey',
            'base_m': 79.2,
            'source': 'Public historical-v1 mathematical test fixture; no observed altitude',
            'height_reference': 'Synthetic model altitude; vertical datum unverified',
            'seed': 23, 'max_error_m': 3.3,
            'noise_model': 'bounded smooth temporal GPS observation error',
            'waves': [(.65, rng.uniform(35, 65), rng.uniform(0, 2 * math.pi)),
                      (.35, rng.uniform(95, 160), rng.uniform(0, 2 * math.pi))],
        }
        hashes = {
            1: '33445391f864cfabd8fee03cb2dec0275b36aed76be052d0a751a4c93c4aad27',
            2: '9e2574d169eed22a36c58233e7b49521de9a7b923bee37811d9f4af05937e5c5',
            3: 'a7ea98bffb5b5b8107d4714a1ed8dfd4770045e90d8a89e3d5123643d70d4397',
        }
        for version, expected in hashes.items():
            with self.subTest(version=version):
                samples, metadata = build_samples(self.data, FakeReference(), distance=1100, pace=250,
                                                  cadence=185, seed=1067074935, model_version=version,
                                                  elevation=elevation)
                serialized = json.dumps([samples, metadata], sort_keys=True, separators=(',', ':'))
                self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(), expected)

    def test_actual_1200_m_fast_plan_retains_running_variation(self):
        law = TimeLaw(self.path, 1200, 200, seed=3623146916, profile_version=1)
        def average_speed(start, end):
            return statistics.mean(speed_at_distance(law, s) for s in range(start, end))
        self.assertGreater(average_speed(30, 216), average_speed(516, 1000) + .3)
        self.assertGreater(1000 / average_speed(516, 1000) - 1000 / average_speed(1100, 1160), 15)
        window_paces = [10000 / (law.at(t + 10)[0] - law.at(t)[0]) for t in range(10, 220, 10)]
        self.assertGreater(max(window_paces) - min(window_paces), 25)
        self.assertLess(law.at(208.45)[0], law.profile['phases']['finish_start_m'])
        self.assertAlmostEqual(law.times[-1], 240)

    def test_bends_slow_same_effort_with_same_seed_and_smooth_anticipation(self):
        # Controlled geometry isolates curvature from effort/seed: the alternate
        # path reports no curvature but retains the identical route and knots.
        class StraightCurvature:
            length = self.path.length
            distances = self.path.distances
            def at(inner, distance):
                point, heading, _ = self.path.at(distance)
                return point, heading, 0.0
        for seed in range(10):
            law = TimeLaw(self.path, 1050, 250, seed=seed)
            straight = TimeLaw(StraightCurvature(), 1050, 250, seed=seed)
            bends, straights = [], []
            for s in range(450, 800):
                relative = speed_at_distance(law, s)/speed_at_distance(straight, s)
                (bends if self.path.at(s)[2] else straights).append(relative)
            self.assertLess(statistics.mean(bends), statistics.mean(straights)-.05)
            # There is no abrupt speed jump at stadium curvature boundaries.
            for boundary in self.path.distances:
                if 450 < boundary+400 < 800:
                    s = boundary+400
                    self.assertLess(abs(speed_at_distance(law, s+.01)-speed_at_distance(law, s-.01)), .01)

    def test_cadence_tracks_speed_and_actual_reference_phase(self):
        samples, metadata = build_samples(self.data, FakeReference(), seed=8)
        moving = [s for s in samples if s['speed_m_s'] > 3]
        slow, fast = min(moving, key=lambda s: s['speed_m_s']), max(moving, key=lambda s: s['speed_m_s'])
        self.assertGreater(fast['cadence_nominal_spm']-slow['cadence_nominal_spm'], 12)
        for previous, sample in zip(samples, samples[1:]):
            expected = (previous['cadence_nominal_spm']+sample['cadence_nominal_spm'])/(2*FakeReference.cadence)
            self.assertAlmostEqual((sample['phase_s']-previous['phase_s'])/(sample['t_s']-previous['t_s']), expected, places=9)
        continued, continued_metadata = build_samples(self.data, FakeReference(), distance=5, offset=1050,
                                                     phase=samples[-1]['phase_s'], seed=8, allow_slower=True)
        self.assertEqual(continued[0]['phase_s'], samples[-1]['phase_s'])
        self.assertEqual(continued[0]['position_m'], samples[-1]['position_m'])
        verify_samples(continued, continued_metadata)

    def test_pace_limits_and_feasibility_attenuation(self):
        for seed in range(12):
            for distance, pace in ((1050, 200), (450, 600), (50, 200), (5, 600)):
                law = TimeLaw(self.path, distance, pace, seed=seed, allow_slower=True)
                self.assertLessEqual(law.duration, 300)
                self.assertGreaterEqual(law.duration+1e-8, distance*pace/1000)
                if (distance, pace) == (1050, 200):
                    self.assertAlmostEqual(law.duration, 210)
                    self.assertTrue(law.profile['attenuated'])
                for time in (0, law.duration*.3, law.duration*.9, law.duration):
                    arc, speed = law.at(time)
                    self.assertTrue(0 <= arc <= distance and 0 <= speed <= 6)
        for seed in (None, '1', 1.5, True):
            with self.subTest(seed=seed), self.assertRaises(MotionError):
                TimeLaw(self.path, 1050, 250, seed=seed)


if __name__ == '__main__':
    unittest.main()
