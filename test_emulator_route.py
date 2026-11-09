"""Offline-only route geometry and cleanup tests; no emulator is contacted."""
import contextlib
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import emulator_route as route


def sample():
    lon, lat = route.STADIUM_SEED
    return {"closed": True, "points": [[lon, lat], [lon + .0002, lat], [lon + .0002, lat + .0002]]}


class RouteTests(unittest.TestCase):
    def test_bad_json_and_nonfinite_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json"
            for content in ['{', '{"closed":true,"points":[[NaN,0],[0,0],[1,1]]}']:
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises((ValueError, route.MotionError)):
                    route.load_route(path)
        for value in [float("nan"), float("inf"), 10 ** 1000]:
            data = sample()
            data["points"][0][0] = value
            with self.subTest(value=str(value)[:10]), self.assertRaises(route.MotionError):
                route.validate_route(data)

    def test_open_duplicate_zero_and_cross_campus_routes_rejected(self):
        cases = []
        data = sample(); data["closed"] = False; cases.append(data)
        data = sample(); data["points"][1] = data["points"][0][:]; cases.append(data)
        data = sample(); data["points"] = [data["points"][0]] * 3; cases.append(data)
        data = sample(); data["points"][1][0] += .002; cases.append(data)
        data = sample(); data["points"][1][0] += .02; cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(route.MotionError):
                route.validate_route(data)

    def test_arc_length_wrap_and_target_duration(self):
        vertices, lengths = route.validate_route(sample())
        perimeter = sum(lengths)
        self.assertAlmostEqual(route.plan_duration(1020, 250), 255)
        self.assertEqual(route.position(vertices, lengths, 0), vertices[0])
        middle = route.position(vertices, lengths, lengths[0] / 2)
        self.assertAlmostEqual(middle[0], (vertices[0][0] + vertices[1][0]) / 2)
        for distance in [0, lengths[0] / 2, 1020]:
            a = route.position(vertices, lengths, distance)
            b = route.position(vertices, lengths, distance + perimeter * 4)
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y)
        with self.assertRaises(route.MotionError):
            route.plan_duration(1020, 600)

    def test_default_validation_never_opens_console(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json"
            path.write_text(json.dumps(sample()))
            with patch.object(route, "Console") as console, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(route.main(["--route", str(path)]), 0)
                self.assertEqual(route.main(["--route", str(path), "--check"]), 0)
            console.assert_not_called()

    def test_raw_nmea_fields_utc_checksum_units_and_order(self):
        console = Mock()
        stamp = datetime(2026, 11, 6, 1, 2, 3, tzinfo=timezone.utc)
        with patch.object(route, "datetime") as clock:
            clock.now.return_value = stamp
            route.gps(console, (-121.5, -38.25), 7.125)
            clock.now.assert_called_once_with(timezone.utc)
        commands = [call.args[0] for call in console.command.call_args_list]
        bodies = []
        for command in commands:
            self.assertTrue(command.startswith("geo nmea $"))
            sentence = command.removeprefix("geo nmea $")
            body, checksum = sentence.split("*")
            calculated = 0
            for byte in body.encode("ascii"):
                calculated ^= byte
            self.assertEqual(checksum, f"{calculated:02X}")
            self.assertNotIn("\n", command)
            bodies.append(body.split(","))
        self.assertEqual(bodies[0], ["GPGGA", "010203", "3815.000000", "S", "12130.000000", "W", "1", "10", "1.0", "50.0", "M", "0.0", "M", "", ""])
        self.assertEqual(bodies[1], ["GPRMC", "010203", "A", "3815.000000", "S", "12130.000000", "W", "7.125000000", "0.000000000", "061126", "0.0", "W"])

    def test_nondefault_altitude_reaches_both_transports(self):
        height = 79.234567
        console = Mock()
        with patch.dict(os.environ, {"SPORTWORLD_GPS_TRANSPORT": "nmea"}):
            route.gps(console, (121.5, 38.8), 1, 90, altitude_m=height)
        sentence = console.command.call_args_list[0].args[0].removeprefix("geo nmea $")
        body, checksum = sentence.split("*")
        self.assertEqual(float(body.split(",")[9]), height)
        self.assertEqual(route.nmea_sentence(body), "$"+sentence)
        with patch.dict(os.environ, {"SPORTWORLD_GPS_TRANSPORT": "grpc"}):
            route.gps(console, (121.5, 38.8), 1, 90, altitude_m=height)
        console.cached_gps.update.assert_called_once_with((121.5, 38.8), 1, 90, height)

    def test_invalid_altitude_rejected_before_transport(self):
        for transport in ("nmea", "grpc"):
            for height in (math.nan, math.inf, -math.inf):
                console = Mock()
                with patch.dict(os.environ, {"SPORTWORLD_GPS_TRANSPORT": transport}), \
                     self.subTest(transport=transport, height=height), self.assertRaises(route.MotionError):
                    route.gps(console, (121.5, 38.8), 1, altitude_m=height)
                console.command.assert_not_called()
                console.cached_gps.update.assert_not_called()

    def test_nmea_zero_boundaries_and_minute_rollover(self):
        self.assertEqual(route.nmea_coordinate(0, 2), "0000.000000")
        self.assertEqual(route.nmea_coordinate(0, 3), "00000.000000")
        self.assertEqual(route.nmea_coordinate(12 + 59.9999999 / 60, 2), "1300.000000")
        self.assertEqual(route.nmea_coordinate(180, 3), "18000.000000")
        self.assertEqual(route.nmea_coordinate(-90, 2), "9000.000000")
        console = Mock()
        route.gps(console, (0, 0), 0)
        self.assertIn(",0000.000000,N,00000.000000,E,", console.command.call_args_list[0].args[0])
        for point, speed in (((181, 0), 0), ((0, 91), 0), ((float("nan"), 0), 0), ((0, 0), float("inf")), ((0, 0), -1)):
            with self.subTest(point=point, speed=speed), self.assertRaises(route.MotionError):
                route.gps(console, point, speed)

    def test_interrupt_and_command_failure_cleanup_use_fresh_identity(self):
        vertices, lengths = route.validate_route(sample())
        baseline = {"acceleration": (0, 9.8, 0), "gyroscope": (0, 0, 0)}
        args = SimpleNamespace(distance=1020, pace=250, cadence=175)
        for failure in [route.StopRequested(signal.SIGINT), route.StopRequested(signal.SIGTERM),
                        route.MotionError("offline injected KO")]:
            events = []

            class FakeConsole:
                def __init__(self, port):
                    self.index = len([e for e in events if e[0] == "connect"])
                    events.append(("connect", self.index, port))
                def command(self, command):
                    events.append(("command", self.index, command))
                    if command == "avd name": return route.AVD
                    if command.startswith("geo nmea") and self.index == 0: raise failure
                    return ""
                def close(self): events.append(("close", self.index))

            def fake_restore(port, avd, original):
                events.append(("restore", port, avd, original))

            with self.subTest(failure=type(failure).__name__), \
                 patch.object(route, "Console", FakeConsole), \
                 patch.object(route, "values", side_effect=lambda c, n: baseline[n]), \
                 patch.object(route, "restore", side_effect=fake_restore), \
                 contextlib.redirect_stdout(io.StringIO()):
                if isinstance(failure, route.MotionError):
                    with self.assertRaises(route.MotionError): route.execute(vertices, lengths, args)
                else:
                    self.assertEqual(route.execute(vertices, lengths, args), 128 + failure.signum)
            self.assertIn(("close", 0), events)
            commands = [event[2] for event in events if event[0] == "command" and event[1] == 1]
            self.assertEqual(commands[0], "avd name")
            self.assertTrue(commands[1].startswith("geo nmea $GPGGA,"))
            self.assertTrue(commands[2].startswith("geo nmea $GPRMC,"))
            self.assertEqual(float(commands[2].split(",")[7]), 0)
            self.assertIn(("restore", route.PORT, route.AVD, baseline), events)
            self.assertLess(events.index(("close", 0)), events.index(("connect", 1, route.PORT)))

    def test_gps_stop_failure_still_attempts_sensor_restoration(self):
        class WrongDevice:
            def __init__(self, port): pass
            def command(self, command): return "not-the-tablet"
            def close(self): raise OSError("offline injected close failure")
        with patch.object(route, "Console", WrongDevice), patch.object(route, "restore") as restore:
            with self.assertRaisesRegex(route.MotionError, "CLEANUP FAILED"):
                route.cleanup(route.STADIUM_SEED, {"test": "offline"})
            restore.assert_called_once_with(route.PORT, route.AVD, {"test": "offline"})


if __name__ == "__main__":
    unittest.main()
