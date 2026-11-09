from enum import Enum
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import emulator_gps as gps
from emulator_motion import MotionError


class GpsProtocolTests(unittest.TestCase):
    def test_cached_fix_encodes_speed_units_and_wrapped_bearing(self):
        wire = gps.encode_fix((121.5, 38.8), 7.125, -90)
        self.assertEqual(wire[:2], b"\x08\x01")
        self.assertEqual(wire[-2:], b"\x38\x06")
        decoded = {wire[i] >> 3: struct.unpack_from("<d", wire, i + 1)[0]
                   for i in range(2, len(wire) - 2, 9)}
        self.assertEqual(decoded[2], 38.8)
        self.assertEqual(decoded[3], 121.5)
        self.assertAlmostEqual(decoded[4], 7.125 * 1852 / 3600)
        self.assertEqual(decoded[5], 270)
        self.assertEqual(decoded[6], 50)

    def test_nondefault_altitude_is_encoded_and_invalid_height_rejected(self):
        wire = gps.encode_fix((121.5, 38.8), 7.125, -90, altitude_m=79.234567)
        decoded = {wire[i] >> 3: struct.unpack_from("<d", wire, i + 1)[0]
                   for i in range(2, len(wire) - 2, 9)}
        self.assertEqual(decoded[6], 79.234567)
        for height in (math.nan, math.inf, -math.inf):
            with self.subTest(height=height), self.assertRaises(MotionError):
                gps.encode_fix((121.5, 38.8), 0, 0, altitude_m=height)

    def test_invalid_fix_is_rejected_before_transport(self):
        for point, speed, bearing in (((181, 0), 0, 0), ((0, 91), 0, 0),
                                      ((0, 0), -1, 0), ((0, 0), 1, math.nan)):
            with self.subTest(point=point, speed=speed, bearing=bearing):
                with self.assertRaises(MotionError):
                    gps.encode_fix(point, speed, bearing)

    def test_discovery_requires_both_exact_avd_and_console_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "pid_1.ini").write_text("avd.name=other\nport.serial=15556\ngrpc.port=18556\ngrpc.token=test-only\n")
            with patch.object(gps, "DISCOVERY", path), \
                 patch.object(gps, "verify_emulator_owner", return_value=2), \
                 patch.object(gps, "listener_pid", return_value=2):
                with self.assertRaises(MotionError):
                    gps.discover("sportworld_tablet34", 15556)
                (path / "pid_2.ini").write_text("avd.name=sportworld_tablet34\nport.serial=15556\ngrpc.port=18556\ngrpc.token=test-only\n")
                self.assertEqual(gps.discover("sportworld_tablet34", 15556), (18556, "test-only"))
                with patch.object(gps, "verify_emulator_owner", side_effect=MotionError("foreign")), \
                     self.assertRaises(MotionError):
                    gps.discover("sportworld_tablet34", 5554)

    def test_same_named_foreign_discovery_is_never_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for pid in (1, 2):
                (path / f"pid_{pid}.ini").write_text("avd.name=sportworld_tablet34\nport.serial=15556\ngrpc.port=18556\ngrpc.token=test-only\n")
            original_read = Path.read_text
            def guarded_read(filename, *args, **kwargs):
                self.assertEqual(filename.name, "pid_2.ini")
                return original_read(filename, *args, **kwargs)
            with patch.object(gps, "DISCOVERY", path), \
                 patch.object(gps, "verify_emulator_owner", return_value=2), \
                 patch.object(gps, "listener_pid", return_value=2), \
                 patch.object(Path, "read_text", autospec=True, side_effect=guarded_read):
                self.assertEqual(gps.discover("sportworld_tablet34", 15556), (18556, "test-only"))

    def test_foreign_grpc_listener_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "pid_2.ini").write_text("avd.name=sportworld_tablet34\nport.serial=15556\ngrpc.port=18556\ngrpc.token=test-only\n")
            with patch.object(gps, "DISCOVERY", path), \
                 patch.object(gps, "verify_emulator_owner", return_value=2), \
                 patch.object(gps, "listener_pid", return_value=3), self.assertRaises(MotionError):
                gps.discover("sportworld_tablet34", 15556)

    def test_foreign_emulator_is_rejected_before_reading_discovery(self):
        with patch.object(gps, "verify_emulator_owner", side_effect=MotionError("foreign")), \
             patch.object(Path, "read_text") as read, self.assertRaises(MotionError):
            gps.discover("sportworld_tablet34", 15556)
        read.assert_not_called()


class GpsRetryTests(unittest.TestCase):
    """Offline RPC doubles exercise retry boundaries without device access."""
    class StatusCode(Enum):
        DEADLINE_EXCEEDED = 1
        UNAVAILABLE = 2
        UNAUTHENTICATED = 3
        PERMISSION_DENIED = 4
        INVALID_ARGUMENT = 5

    class RpcError(Exception):
        def __init__(self, status):
            self.status = status

        def code(self):
            return self.status

    def setUp(self):
        self.transport = gps.CachedGps.__new__(gps.CachedGps)
        self.transport.grpc = SimpleNamespace(RpcError=self.RpcError, StatusCode=self.StatusCode)
        self.transport.metadata = (("authorization", "Bearer test-only"),)
        self.transport.send = Mock()

    def test_success_sends_once_with_original_timeout_and_positional_altitude(self):
        with patch.object(gps.time, "monotonic", return_value=10):
            self.transport.update((121.5, 38.8), 2, 90, 79.4)
        self.transport.send.assert_called_once_with(
            gps.encode_fix((121.5, 38.8), 2, 90, 79.4), metadata=self.transport.metadata, timeout=.15)

    def test_transient_failure_retries_the_same_payload_once_with_remaining_budget(self):
        for status in (self.StatusCode.DEADLINE_EXCEEDED, self.StatusCode.UNAVAILABLE):
            with self.subTest(status=status):
                self.transport.send.reset_mock(side_effect=True)
                self.transport.send.side_effect = [self.RpcError(status), None]
                with patch.object(gps.time, "monotonic", side_effect=[10, 10.17]):
                    self.transport.update((121.5, 38.8), 2, 90, 79.4)
                first, second = self.transport.send.call_args_list
                self.assertIs(first.args[0], second.args[0])
                self.assertIs(first.kwargs["metadata"], second.kwargs["metadata"])
                self.assertEqual(first.kwargs["timeout"], .15)
                self.assertAlmostEqual(second.kwargs["timeout"], .03)
                self.assertLessEqual(second.kwargs["timeout"], .05)

    def test_early_transient_failure_keeps_retry_timeout_at_most_fifty_milliseconds(self):
        self.transport.send.side_effect = [self.RpcError(self.StatusCode.UNAVAILABLE), None]
        with patch.object(gps.time, "monotonic", side_effect=[10, 10.01]):
            self.transport.update((121.5, 38.8), 0, 0)
        self.assertEqual(self.transport.send.call_args_list[1].kwargs["timeout"], .05)

    def test_second_failure_is_reported_without_a_third_attempt(self):
        self.transport.send.side_effect = [self.RpcError(self.StatusCode.UNAVAILABLE),
                                           self.RpcError(self.StatusCode.DEADLINE_EXCEEDED)]
        with patch.object(gps.time, "monotonic", side_effect=[10, 10.15, 10.20]), \
                self.assertRaisesRegex(MotionError, "DEADLINE_EXCEEDED"):
            self.transport.update((121.5, 38.8), 0, 0)
        self.assertEqual(self.transport.send.call_count, 2)

    def test_exhausted_total_budget_does_not_retry(self):
        for elapsed in (.20, .25):
            with self.subTest(elapsed=elapsed):
                self.transport.send.reset_mock(side_effect=True)
                self.transport.send.side_effect = self.RpcError(self.StatusCode.DEADLINE_EXCEEDED)
                with patch.object(gps.time, "monotonic", side_effect=[10, 10+elapsed]), \
                        self.assertRaisesRegex(MotionError, "DEADLINE_EXCEEDED"):
                    self.transport.update((121.5, 38.8), 0, 0)
                self.assertEqual(self.transport.send.call_count, 1)

    def test_authentication_and_other_permanent_errors_do_not_retry(self):
        for status in (self.StatusCode.UNAUTHENTICATED, self.StatusCode.PERMISSION_DENIED,
                       self.StatusCode.INVALID_ARGUMENT):
            with self.subTest(status=status):
                self.transport.send.reset_mock(side_effect=True)
                self.transport.send.side_effect = self.RpcError(status)
                with patch.object(gps.time, "monotonic", return_value=10), \
                        self.assertRaisesRegex(MotionError, status.name):
                    self.transport.update((121.5, 38.8), 0, 0)
                self.assertEqual(self.transport.send.call_count, 1)

    def test_close_still_closes_the_existing_channel(self):
        self.transport.channel = Mock()
        self.transport.close()
        self.transport.channel.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
