"""Offline regressions; sockets, credentials and sensor state are test doubles."""
import argparse
import contextlib
import io
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import emulator_motion as motion


class FakeServer:
    def __init__(self, banner_path, failure=None):
        self.banner_path = banner_path
        self.failure = failure
        self.original = {"acceleration": (0.0, 9.77631, 0.812349),
                         "gyroscope": (0.0, 0.0, 0.0)}
        self.state = self.original.copy()
        self.connections = []
        self.identity = "sportworld_tablet34"
        self.recovery_identity = None

    def connect(self, address, timeout):
        assert address == ("127.0.0.1", 15556)
        connection = FakeSocket(self, len(self.connections))
        self.connections.append(connection)
        return connection


class FakeSocket:
    def __init__(self, server, index):
        self.server, self.index = server, index
        self.pending = ("Android Console: Authentication required\r\n'" +
                        str(server.banner_path) + "'\r\nOK\r\n").encode()
        self.interrupt_pending = False
        self.closed = False
        self.unread_when_closed = b""
        self.commands = []

    def settimeout(self, value):
        pass

    def setsockopt(self, level, option, value):
        assert (level, option, value) == (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def sendall(self, data):
        command = data.decode().strip()
        # Do not retain test authentication commands or their token values.
        if command.startswith("auth "):
            reply = "OK\r\n"
        else:
            self.commands.append(command)
            if command == "avd name":
                identity = (self.server.recovery_identity if self.index > 0 else None)
                reply = (identity or self.server.identity) + "\r\nOK\r\n"
            elif command.startswith("sensor get "):
                sensor = command.split()[2]
                reply = sensor + " = " + ":".join(map(str, self.server.state[sensor])) + "\r\nOK\r\n"
            elif command.startswith("sensor set "):
                _, _, sensor, vector = command.split()
                self.server.state[sensor] = tuple(map(float, vector.split(":")))
                reply = "OK\r\n"
                if self.index == 0 and self.server.failure and sensor == "acceleration":
                    self.interrupt_pending = True
            else:
                raise AssertionError("Unexpected test command")
        self.pending += reply.encode()

    def recv(self, count):
        if self.interrupt_pending:
            self.interrupt_pending = False
            failure = self.server.failure
            self.server.failure = None
            if failure == "ctrl-c":
                raise KeyboardInterrupt
            if failure == "timeout":
                raise socket.timeout("offline injected timeout")
            if failure == "ko":
                self.pending = b"KO: offline injected rejection\r\n"
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    def close(self):
        if not self.closed:
            self.unread_when_closed = self.pending
        self.closed = True


class MotionTests(unittest.TestCase):
    def setUp(self):
        ownership = patch.object(motion, "verify_emulator_owner", return_value=123)
        ownership.start()
        self.addCleanup(ownership.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "lab"
        self.root.mkdir()
        self.token_path = self.root / "console-auth.token"
        self.token_path.write_text("offline-test-credential")
        self.external_path = Path(self.temp.name) / "external-do-not-read"
        self.external_path.write_text("private-sentinel")
        self.args = SimpleNamespace(port=15556, seconds=0.1, cadence=175,
                                    rate=20, acceleration=1.5, gyroscope=0.15)

    def execute(self, server):
        output = io.StringIO()
        with patch.object(motion, "ROOT", self.root), \
             patch.object(motion.socket, "create_connection", side_effect=server.connect), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            return motion.run(self.args), output.getvalue()

    def test_external_banner_reads_only_fixed_isolated_copy(self):
        server = FakeServer(self.external_path)
        original_read = Path.read_text
        read_paths = []

        def guarded_read(path, *args, **kwargs):
            read_paths.append(path)
            self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=guarded_read):
            code, _ = self.execute(server)
        self.assertEqual(code, 0)
        self.assertEqual(read_paths, [self.token_path.resolve(), self.token_path.resolve()])
        self.assertEqual(server.state, server.original)

    def test_missing_copy_fails_without_global_fallback(self):
        self.token_path.unlink()
        server = FakeServer(self.external_path)
        with self.assertRaisesRegex(motion.MotionError, "no global fallback"):
            self.execute(server)
        self.assertEqual(server.connections[0].commands, [])
        self.assertEqual(server.state, server.original)

    def test_symlink_copy_outside_lab_is_refused(self):
        self.token_path.unlink()
        self.token_path.symlink_to(self.external_path)
        with self.assertRaisesRegex(motion.MotionError, "outside the lab"):
            self.execute(FakeServer(self.external_path))

    def test_ctrl_c_after_send_before_ok_restores_on_new_connection(self):
        server = FakeServer(self.external_path, "ctrl-c")
        code, output = self.execute(server)
        self.assertEqual(code, 130)
        self.assertEqual(len(server.connections), 2)
        self.assertIn(b"OK", server.connections[0].unread_when_closed)
        self.assertEqual(server.connections[1].commands[0], "avd name")
        self.assertEqual(server.state, server.original)
        self.assertIn("Restored and verified", output)

    def test_timeout_after_send_restores_and_propagates_failure(self):
        server = FakeServer(self.external_path, "timeout")
        with self.assertRaises(socket.timeout):
            self.execute(server)
        self.assertEqual(len(server.connections), 2)
        self.assertEqual(server.state, server.original)

    def test_ko_stops_input_and_restores(self):
        server = FakeServer(self.external_path, "ko")
        with self.assertRaisesRegex(motion.MotionError, "rejected"):
            self.execute(server)
        self.assertEqual(len(server.connections), 2)
        self.assertEqual(server.state, server.original)
        writes = [c for c in server.connections[0].commands if c.startswith("sensor set")]
        self.assertEqual(len(writes), 1)

    def test_unexpected_avd_never_receives_sensor_writes(self):
        server = FakeServer(self.external_path)
        server.identity = "someone-elses-avd"
        with self.assertRaisesRegex(motion.MotionError, "unexpected AVD"):
            self.execute(server)
        self.assertEqual(server.connections[0].commands, ["avd name"])

    def test_changed_avd_during_recovery_is_not_written(self):
        server = FakeServer(self.external_path, "ctrl-c")
        server.recovery_identity = "someone-elses-avd"
        with self.assertRaisesRegex(motion.MotionError, "RESTORE FAILED.*AVD changed"):
            self.execute(server)
        self.assertEqual(server.connections[1].commands, ["avd name"])

    def test_bounds_reject_nonfinite_or_excessive_values(self):
        for text in ["nan", "inf", "-1", "301"]:
            with self.subTest(value=text), self.assertRaises(argparse.ArgumentTypeError):
                motion.bounded(0.1, 300)(text)


class ConsoleBatchTests(unittest.TestCase):
    """Socket doubles exercise framing and pipelining without device connections."""
    def console(self, replies):
        connection = motion.Console.__new__(motion.Console)
        connection.socket = Mock()
        connection.socket.recv.side_effect = replies
        connection.buffer = b""
        connection.closed = False
        return connection

    def test_batch_sends_once_and_consumes_coalesced_responses(self):
        connection = self.console([b"first\r\nOK\r\nsecond\r\nOK\r\n"])
        self.assertEqual(connection.commands(("one", "two")), ["first", "second"])
        connection.socket.sendall.assert_called_once_with(b"one\ntwo\n")
        self.assertEqual(connection.socket.recv.call_count, 1)
        self.assertEqual(connection.buffer, b"")
        connection.socket.close.assert_not_called()

    def test_fragmented_responses_and_following_single_command_keep_alignment(self):
        connection = self.console([b"firs", b"t\r\nO", b"K\r\nsec", b"ond\r\nOK\r\n", b"third\r\nOK\r\n"])
        self.assertEqual(connection.commands(("one", "two")), ["first", "second"])
        self.assertEqual(connection.command("three"), "third")
        self.assertEqual(connection.socket.sendall.call_count, 2)
        self.assertEqual(connection.buffer, b"")

    def test_all_seven_sensor_acknowledgments_are_consumed(self):
        from sensor_channels import COUPLED_SENSORS
        connection = self.console([b"OK\r\n"*7])
        mapping = {sensor: (1, 2, 3) for sensor in COUPLED_SENSORS}
        motion.set_sensor_values(connection, mapping)
        payload = connection.socket.sendall.call_args.args[0]
        self.assertEqual(payload.decode().splitlines(),
                         ["sensor set "+sensor+" 1:2:3" for sensor in COUPLED_SENSORS])
        connection.socket.sendall.assert_called_once()
        self.assertEqual(connection.buffer, b"")
        self.assertEqual(connection.socket.recv.call_count, 1)

    def test_invalid_command_batch_writes_nothing(self):
        for commands in (("valid", "bad\ncommand"), ("valid", "bad\rcommand"), ("valid", "非ASCII")):
            connection = self.console([])
            with self.subTest(commands=commands), self.assertRaises((motion.MotionError, UnicodeEncodeError)):
                connection.commands(commands)
            connection.socket.sendall.assert_not_called()
            connection.socket.close.assert_not_called()
            self.assertFalse(connection.closed)

    def test_invalid_sensor_frame_writes_nothing_including_valid_prefix(self):
        for sensor, vector in (("unknown", (1, 2, 3)), ("gyroscope", (1, 2)),
                               ("gyroscope", (1, 2, float("nan"))),
                               ("gyroscope", (1, 2, float("inf")))):
            connection = self.console([])
            with self.subTest(sensor=sensor, vector=vector), self.assertRaises(motion.MotionError):
                motion.set_sensor_values(connection, {"acceleration": (1, 2, 3), sensor: vector})
            connection.socket.sendall.assert_not_called()

    def test_batch_failure_closes_connection_and_rejects_later_commands(self):
        for failure in (motion.MotionError, socket.timeout, KeyboardInterrupt):
            replies = ([b"KO: rejected\r\nOK\r\n"] if failure is motion.MotionError else
                       [failure("offline injected failure")])
            connection = self.console(replies)
            with self.subTest(failure=failure), self.assertRaises(failure):
                connection.commands(("one", "two"))
            self.assertTrue(connection.closed)
            connection.socket.close.assert_called_once()
            with self.assertRaisesRegex(motion.MotionError, "closed"):
                connection.command("later")
            connection.socket.sendall.assert_called_once()

    def test_partial_write_failure_also_closes_connection(self):
        connection = self.console([])
        connection.socket.sendall.side_effect = socket.timeout("offline partial write")
        with self.assertRaises(socket.timeout):
            connection.commands(("one", "two"))
        self.assertTrue(connection.closed)
        connection.socket.close.assert_called_once()

    def test_empty_batch_does_not_write_and_close_remains_idempotent(self):
        connection = self.console([])
        self.assertEqual(connection.commands([]), [])
        motion.set_sensor_values(connection, {})
        connection.socket.sendall.assert_not_called()
        connection.close()
        connection.close()
        connection.socket.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
