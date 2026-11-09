"""Offline continuation checks, using the existing explicit motion fixture."""
import copy
import unittest
from unittest.mock import Mock

from emulator_motion import MotionError
from resume_outdoor import remaining_plan
from run_trajectory import build_samples, DEFAULT_GRAVITY
from test_coupled_motion import FakeReference, route_fixture
from run_session import transmit


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = route_fixture()
        cls.reference = FakeReference()
        samples, plan = build_samples(cls.data, cls.reference, 1050, 250, 175, seed=7)
        cls.anchor = samples[2000]
        cls.original = {"mode": "outdoor", "state": "failed", "pace_profile_seed": 7,
                        "segments": [{"planned": plan, "restoration_verified": True,
                                      "input_elapsed_s": 100, "sent_distance_m": cls.anchor["distance_m"],
                                      "last_point": [cls.anchor["longitude"], cls.anchor["latitude"]]}]}
        cls.baseline = {"acceleration": DEFAULT_GRAVITY, "gyroscope": (0, 0, 0)}

    def test_continuation_keeps_last_gps_position_and_exact_remaining_arc(self):
        samples, plan, used_s, used_m = remaining_plan(self.original, self.data, self.reference, self.baseline)
        self.assertAlmostEqual(plan["offset_m"], self.anchor["arc_m"])
        self.assertAlmostEqual(plan["offset_m"] + plan["distance_m"], 1050)
        self.assertAlmostEqual(samples[0]["longitude"], self.anchor["longitude"])
        self.assertAlmostEqual(samples[0]["latitude"], self.anchor["latitude"])
        self.assertAlmostEqual(samples[0]["phase_s"], self.anchor["phase_s"])
        self.assertEqual(samples[0]["altitude_m"], self.anchor["altitude_m"])
        self.assertEqual(samples[0]["absolute_time_s"], self.anchor["absolute_time_s"])
        self.assertLessEqual(used_s + plan["duration_s"], 300)
        self.assertLessEqual(used_m + plan["distance_m"], 1200)

    def test_campus_or_finished_or_unrestored_runs_cannot_continue(self):
        for key, value in (("mode", "campus"), ("state", "reviewed")):
            state = copy.deepcopy(self.original)
            state[key] = value
            with self.assertRaises(MotionError):
                remaining_plan(state, self.data, self.reference, self.baseline)
        state = copy.deepcopy(self.original)
        state["segments"][0]["restoration_verified"] = False
        with self.assertRaises(MotionError):
            remaining_plan(state, self.data, self.reference, self.baseline)

    def test_cumulative_time_and_distance_caps_survive_continuation(self):
        for key, value in (("input_elapsed_s", 299), ("sent_distance_m", 1199),
                           ("input_elapsed_s", float("nan"))):
            state = copy.deepcopy(self.original)
            state["segments"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(MotionError):
                remaining_plan(state, self.data, self.reference, self.baseline)

    def test_unknown_anchor_is_rejected_instead_of_teleporting(self):
        state = copy.deepcopy(self.original)
        state["segments"][0]["last_point"] = [120, 38]
        with self.assertRaisesRegex(MotionError, "中断位置"):
            remaining_plan(state, self.data, self.reference, self.baseline)

    def test_constant_observation_offset_is_preserved_when_finding_anchor(self):
        state = copy.deepcopy(self.original)
        offset = [0.000004, -0.000003]
        state['segments'][0]['gps_offset_degrees'] = offset
        state['segments'][0]['last_point'] = [self.anchor['longitude'] + offset[0], self.anchor['latitude'] + offset[1]]
        samples, plan, _, _ = remaining_plan(state, self.data, self.reference, self.baseline)
        self.assertAlmostEqual(samples[0]['longitude'], self.anchor['longitude'])
        self.assertAlmostEqual(samples[0]['latitude'], self.anchor['latitude'])
        self.assertAlmostEqual(plan['offset_m'], self.anchor['arc_m'])

    def test_invalid_or_large_observation_offset_is_rejected(self):
        for offset in ([1], [float('nan'), 0], [True, 0], ['bad', 0], [0.01, 0]):
            state = copy.deepcopy(self.original)
            state['segments'][0]['gps_offset_degrees'] = offset
            with self.subTest(offset=offset), self.assertRaises(MotionError):
                remaining_plan(state, self.data, self.reference, self.baseline)

    def test_actual_cumulative_time_limit_stops_the_sender(self):
        class Clock:
            value = 0
            def __call__(self):
                return self.value
            def sleep(self, seconds):
                self.value += seconds
        clock = Clock()
        samples = [{"t_s": i / 20, "longitude": 121.5, "latitude": 38.8,
                    "speed_m_s": 0, "distance_m": 0, "acceleration": DEFAULT_GRAVITY,
                    "gyroscope": (0, 0, 0)} for i in range(4)]
        metrics = {"elapsed_before_segment_s": 299.95, "missed_frames": 0,
                   "frames": 0, "gps_samples": 0, "max_lateness_s": 0, "max_frame_gap_s": 0}
        with self.assertRaisesRegex(MotionError, "300 秒"):
            transmit(samples, Mock(), Mock(), metrics, clock, clock.sleep)


if __name__ == "__main__":
    unittest.main()
