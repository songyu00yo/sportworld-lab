"""Geometric and telemetry regressions with explicit offline fixtures."""
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import motion_reference
import run_session
from run_trajectory import build_samples, verify_samples
from stadium_track import StadiumPath, EXAMPLE_ROUTE, create_example_route, create_neusoft_route
from emulator_route import STADIUM_SEED, validate_route
from emulator_motion import MotionError
from test_coupled_motion import FakeReference
import ui_protocol


def stadium_fixture():
    radius = 36.8
    return {"closed": True, "track": {"kind": "stadium", "center_wgs84": list(STADIUM_SEED),
            "radius_m": radius, "straight_length_m": (400-2*math.pi*radius)/2,
            "axis_degrees_from_east": 6.6, "start_phase_m": 240, "confirmed_lap_m": 400}}


class StadiumTests(unittest.TestCase):
    def setUp(self):
        reference = Path(__file__).resolve().parent / 'examples/elevation-reference.json'
        patcher = patch('elevation_profile.USER_REFERENCE_PATH', reference)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_route_is_a_self_contained_unsurveyed_model(self):
        data = json.loads(EXAMPLE_ROUTE.read_text())
        vertices, lengths = validate_route(data)
        self.assertEqual(data['coordinate_system'], 'WGS84')
        self.assertTrue(data['example'])
        self.assertNotEqual(vertices[0], vertices[-1])
        self.assertAlmostEqual(StadiumPath(data).length, 400)
        self.assertLess(abs(sum(lengths) - 400), .1)
        self.assertEqual(data['checkpoint_markers'], {})
        self.assertNotIn('calibration', data['track'])
        self.assertNotIn('lap_confirmation', data['track'])
        self.assertIn('not field surveyed', data['track']['geometry_status'])
        samples, metadata = build_samples(data, FakeReference())
        self.assertTrue(verify_samples(samples, metadata)['passed'])
        self.assertEqual(metadata['checkpoints'], [])

    def test_route_creation_needs_only_public_example_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'new' / 'route.json'
            created = create_example_route(destination)
            self.assertEqual(created, json.loads(destination.read_text()))
            self.assertEqual(created['checkpoint_distances_m'], {})
            self.assertEqual(create_neusoft_route(destination), created)

    def test_full_lap_is_400m_and_tangent_continuous(self):
        path = StadiumPath(stadium_fixture())
        self.assertAlmostEqual(path.length, 400)
        beginning, first_heading, _ = path.at(0)
        ending, last_heading, _ = path.at(400)
        self.assertLess(motion_reference.norm(tuple(a-b for a, b in zip(beginning, ending))), 1e-10)
        self.assertAlmostEqual(last_heading-first_heading, 2*math.pi)
        for boundary in (0, path.straight, path.straight+math.pi*path.radius, 2*path.straight+math.pi*path.radius):
            distance = (boundary-path.start_phase) % path.length
            before = path.at(distance-1e-5)
            after = path.at(distance+1e-5)
            self.assertLess(abs(after[1]-before[1]), 1e-5)

    def test_only_straights_and_constant_radius_bends(self):
        path = StadiumPath(stadium_fixture())
        for distance in range(400):
            point, _, curvature = path.at(distance)
            self.assertIn(curvature, (0, 1/path.radius))
            self.assertAlmostEqual(path.distance_to(path.coordinates(point)), 0, places=7)

    def test_points_cannot_pull_track_off_the_oval(self):
        data = stadium_fixture()
        before = StadiumPath(data).at(35)
        data["checkpoint_markers"] = {"far_marker": [121.53, 38.895]}
        self.assertEqual(StadiumPath(data).at(35), before)
        data["points"] = [[STADIUM_SEED[0], STADIUM_SEED[1]], [STADIUM_SEED[0]+.0001, STADIUM_SEED[1]], [STADIUM_SEED[0], STADIUM_SEED[1]+.0001]]
        with self.assertRaisesRegex(MotionError, "disagree"):
            StadiumPath(data)

    def test_1050m_on_track_is_2625_laps_with_consistent_inputs(self):
        samples, metadata = build_samples(stadium_fixture(), FakeReference())
        self.assertAlmostEqual(metadata["laps"], 2.625)
        self.assertAlmostEqual(metadata["duration_s"], 262.5)
        self.assertTrue(verify_samples(samples, metadata)["passed"])
        path = StadiumPath(stadium_fixture())
        for sample in samples[::25]:
            self.assertLess(path.distance_to((sample["longitude"], sample["latitude"])), .001)

    def test_bad_declared_lap_is_rejected(self):
        data = stadium_fixture()
        data["track"]["confirmed_lap_m"] = 300
        with self.assertRaises(MotionError):
            StadiumPath(data)

    def test_unknown_app_radius_is_not_a_requirement_or_claimed_as_verified(self):
        data = stadium_fixture()
        track = StadiumPath(data)
        data['checkpoint_markers'] = {
            'mandatory_test': list(track.coordinates(track.at(0)[0])),
            'normal_east': list(track.coordinates((0, 0))),
            'normal_west': list(track.coordinates(track.at(200)[0])),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"route.json"
            path.write_text(json.dumps(data))
            import hashlib
            review = {"date": "2026-11-06", "route_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "confirmed_course": True, "method": "user_checked_app_map", "mandatory_key": "mandatory_test",
                      "checkpoint_keys": list(data["checkpoint_markers"])}
            run_session.atomic_json(run_session.review_path(path), review)
            checked = run_session.validate_route_review(path, data, "2026-11-06")
            self.assertNotIn("app_checkpoint_radius_m", checked)
            self.assertEqual(checked["app_checkpoint_acceptance"], "unverified")
            self.assertGreater(max(checked["checkpoint_distances_m"].values()), 12)

    def test_map_review_is_one_confirmation_without_marker_selection_or_radius(self):
        data = stadium_fixture()
        track = StadiumPath(data)
        data['checkpoint_markers'] = {
            'mandatory_测试必经点': list(track.coordinates(track.at(0)[0])),
            'normal_ne': list(track.coordinates(track.at(100)[0])),
            'normal_sw': list(track.coordinates(track.at(200)[0])),
        }
        data['checkpoint_source'] = 'Offline fixture observed on 2026-11-06'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'route.json'
            path.write_text(json.dumps(data))
            with patch('builtins.input', return_value='已核对') as answer, contextlib.redirect_stdout(io.StringIO()):
                run_session.review_route(path, data)
            self.assertEqual(answer.call_count, 1)
            self.assertIn('2026-11-06', answer.call_args.args[0])
            self.assertIn('不是已确认的今日点位', answer.call_args.args[0])
            review = json.loads(run_session.review_path(path).read_text())
            self.assertIsNone(review['mandatory_key'])
            self.assertEqual(review['review_scope'], 'all_reference_checkpoint_positions')
            self.assertNotIn('app_checkpoint_radius_m', review)
            self.assertEqual(review['app_checkpoint_acceptance'], 'unverified')

    def test_different_map_does_not_create_a_confirmed_review(self):
        data = stadium_fixture()
        data['checkpoint_markers'] = {'mandatory_测试': [121.5295, 38.8945],
                                      'normal_ne': [121.5296, 38.8945], 'normal_sw': [121.5294, 38.8945]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'route.json'
            path.write_text(json.dumps(data))
            with patch('builtins.input', return_value='停止'), self.assertRaises(MotionError):
                run_session.review_route(path, data)
            self.assertFalse(run_session.review_path(path).exists())


class TelemetryTests(unittest.TestCase):
    def test_expected_rms_preserves_energy_when_axes_change_sign(self):
        from motion_report import expected_timeline
        samples = [{"t_s": i*.05, "arc_m": i*.2, "speed_m_s": 4,
                    "cadence_nominal_spm": 175, "acceleration": (0, 9.8*(-1)**i, 0),
                    "gyroscope": (0, 0, 2*(-1)**i)} for i in range(21)]
        timeline = expected_timeline(samples)
        self.assertEqual(len(timeline), 2)
        self.assertAlmostEqual(timeline[1]["acceleration_rms_m_s2"], 9.8)
        self.assertAlmostEqual(timeline[1]["gyro_rms_rad_s"], 2)
        self.assertEqual(timeline[1]["elapsed_s"], 1)

    def test_ui_progress_is_a_separate_structured_event(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("sys.stdin", io.StringIO("confirmed\n")):
            def callback():
                print("offline fixture")
                ui_protocol.publish_progress({"phase": "sending", "distance_m": 12.0})
                self.assertEqual(input("prompt"), "confirmed")
                return 0
            self.assertEqual(ui_protocol.run(callback), 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([message["kind"] for message in messages], ["output", "telemetry", "prompt"])
        self.assertEqual(messages[1]["data"]["distance_m"], 12)
        self.assertIsNone(ui_protocol._event_sink)


if __name__ == "__main__":
    unittest.main()
