"""Offline lifecycle tests with explicit device/process doubles; no app input."""
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import runtime_manager as runtime
import control_panel
import emulator_motion as motion
import run_session as session
from emulator_motion import MotionError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lab = Path(self.temporary.name)
        for name, value in (("LAB", self.lab), ("GNSS_STATE", self.lab / "gnss.json"),
                            ("RUNTIME_STATE", self.lab / "runtime.json")):
            replacement = patch.object(runtime, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(runtime, "RunLock", side_effect=lambda: nullcontext())
        replacement.start()
        self.addCleanup(replacement.stop)
        replacement = patch.object(runtime, "port_open", return_value=False)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.manager = runtime.Runtime(report=Mock())
        self.manager.device = Mock()
        self.manager.map_connection_available = Mock(return_value=True)

    def test_owned_process_selection_excludes_other_avds_and_projects(self):
        (runtime.LAB / "android").mkdir()
        (runtime.LAB / "android/viewer-process.json").write_text(json.dumps({"pid": 13, "serial": runtime.SERIAL}))
        qemu = str(runtime.SDK / "emulator/qemu/darwin-aarch64/qemu-system-aarch64-headless")
        rows = [(10, qemu), (11, qemu), (12, "/elsewhere/qemu-system-aarch64-headless"),
                (13, str(runtime.SCRCPY)), (14, str(runtime.SCRCPY))]
        datadir = str((runtime.LAB / "android/avd/sportworld_tablet34.avd").resolve())
        commands = {10: qemu + " -avd sportworld_tablet34 -ports 15556,15557 -datadir " + datadir,
                    11: qemu + " -avd sportworld_aosp34 -ports 15554,15555",
                    13: str(runtime.SCRCPY) + " --serial=emulator-15556",
                    14: str(runtime.SCRCPY) + " --serial=another-device"}
        def ps(args, **kwargs):
            return SimpleNamespace(stdout=commands[int(args[3])], returncode=0)
        with patch.object(runtime, "process_list", return_value=rows), patch.object(runtime.subprocess, "run", side_effect=ps):
            self.assertEqual(runtime.owned_pids("emulator"), [10])
            self.assertEqual(runtime.owned_pids("viewer"), [13])

    def test_lifecycle_lock_rejects_duplicate_launch_without_unlocking_owner(self):
        with runtime.lifecycle_lock():
            for _ in range(2):
                with self.assertRaisesRegex(MotionError, "正在启动或退出"):
                    with runtime.lifecycle_lock():
                        self.fail("duplicate lifecycle lock acquired")
        with runtime.lifecycle_lock():
            pass

    def test_active_job_reopens_without_touching_guest_or_location(self):
        panel = Mock()
        panel.call.return_value = {"job": {"state": "waiting"}}
        with patch.object(runtime.Panel, "current", return_value=panel), \
             patch.object(self.manager, "launch_guest") as guest, \
             patch.object(self.manager, "prepare_location") as prepare, \
             patch.object(control_panel, "launch") as launch:
            self.manager.start(browser=False)
        guest.assert_not_called()
        prepare.assert_not_called()
        self.manager.device.adb.assert_not_called()
        launch.assert_called_once_with(open_browser=False)

    def test_closing_panel_cannot_be_reopened_as_ready(self):
        panel = Mock()
        panel.call.return_value = {"closing": True}
        with patch.object(runtime.Panel, "current", return_value=panel), \
             patch.object(control_panel, "launch") as launch:
            with self.assertRaisesRegex(MotionError, "正在彻底退出"):
                self.manager.start()
        launch.assert_not_called()

    def test_cold_start_prepares_guest_before_launching_panel(self):
        operations = []
        self.manager.launch_guest = lambda: operations.append("guest")
        self.manager.booted = lambda: True
        self.manager.prepare_location = lambda: operations.append("location")
        self.manager.prepare_network = lambda: operations.append("network")
        self.manager.initialize_location = lambda: operations.append("stationary_location")
        self.manager.prepare_static_sensors = lambda: operations.append("stationary_sensors")
        self.manager.device.activity.return_value = runtime.PACKAGE + "/.mvi.home.HomeActivity"
        with patch.object(runtime.Panel, "current", return_value=None), \
             patch("emulator_gps.discover", side_effect=lambda *args: operations.append("grpc")), \
             patch.object(control_panel, "launch", side_effect=lambda **kwargs: operations.append("panel")):
            self.manager.start(browser=False)
        self.assertEqual(operations, ["guest", "location", "network", "grpc", "stationary_location", "stationary_sensors", "panel"])
        self.assertEqual(json.loads(runtime.RUNTIME_STATE.read_text())["phase"], "ready")

    def test_failed_preparation_never_reports_ready_or_launches_panel(self):
        self.manager.launch_guest = Mock()
        self.manager.booted = lambda: True
        self.manager.prepare_location = Mock(side_effect=MotionError("恢复失败"))
        with patch.object(runtime.Panel, "current", return_value=None), patch.object(control_panel, "launch") as launch:
            with self.assertRaises(MotionError):
                self.manager.start()
        launch.assert_not_called()
        self.assertNotEqual(json.loads(runtime.RUNTIME_STATE.read_text())["phase"], "ready")

    def test_missing_app_requests_manual_install_before_location_changes(self):
        self.manager.launch_guest = Mock()
        self.manager.booted = lambda: True
        self.manager.device.adb.return_value = ""
        with patch.object(runtime.Panel, "current", return_value=None), \
             patch.object(self.manager, "prepare_location") as prepare, \
             patch.object(control_panel, "launch") as launch, \
             self.assertRaisesRegex(MotionError, "手动安装官方"):
            self.manager.start()
        prepare.assert_not_called()
        launch.assert_not_called()

    def test_missing_gnss_component_has_actionable_instructions(self):
        with patch.object(runtime, "GNSS_BINARY", self.lab / "missing-gnss"), \
             self.assertRaisesRegex(MotionError, "patch_gnss.py"):
            self.manager.prepare_location()
        self.manager.device.adb.assert_not_called()

    def test_optional_viewer_is_not_required_to_use_native_window(self):
        with patch.object(runtime, "SCRCPY", self.lab / "missing-scrcpy"), \
             patch.object(runtime, "owned_pids", return_value=[]), \
             patch.object(runtime.shutil, "which", return_value=None), \
             self.assertRaisesRegex(MotionError, "Android 模拟器窗口"):
            self.manager.open_viewer()
        self.manager.device.adb.assert_not_called()

    def test_unrelated_console_is_rejected_without_killing_it(self):
        self.manager.connected = lambda: True
        with patch.object(runtime.Panel, "current", return_value=None), \
             patch.object(runtime, "owned_pids", return_value=[]), \
             patch.object(runtime, "Console") as console:
            console.return_value.command.return_value = "another-avd"
            with self.assertRaisesRegex(MotionError, "不是本项目"):
                self.manager.stop()
        self.manager.device.adb.assert_not_called()

    def test_stop_waits_for_input_before_guest_and_closes_server_last(self):
        operations = []
        panel = Mock()
        panel.state = {"port": 18762}
        panel.call.side_effect = lambda path, *args: operations.append(path) or {"job": {"state": "finished"}}
        self.manager.connected = lambda: True
        self.manager.identify = lambda: operations.append("identity")
        self.manager.device.adb.side_effect = lambda *args, **kwargs: operations.append(args)
        with patch.object(runtime.Panel, "current", return_value=panel), \
             patch.object(runtime, "owned_pids", return_value=[]), \
             patch.object(runtime, "port_open", return_value=False):
            self.manager.stop()
        self.assertLess(operations.index("/api/drain"), operations.index(("emu", "kill")))
        self.assertLess(operations.index("/api/status"), operations.index(("emu", "kill")))
        self.assertEqual(operations[-1], "/api/close-server")
        self.assertEqual(json.loads(runtime.RUNTIME_STATE.read_text())["phase"], "stopped")

    def test_guest_shutdown_timeout_preserves_panel_and_cancels_draining(self):
        panel = Mock()
        panel.state = {"port": 18762}
        panel.call.return_value = {"job": {"state": "finished"}}
        self.manager.connected = lambda: False
        def wait(check, seconds, message):
            if "测试平板尚未退出" in message:
                raise MotionError(message)
            return True
        with patch.object(runtime.Panel, "current", return_value=panel), \
             patch.object(runtime, "owned_pids", return_value=[]), \
             patch.object(runtime, "wait_until", side_effect=wait):
            with self.assertRaisesRegex(MotionError, "控制台已保留"):
                self.manager.stop()
        paths = [call.args[0] for call in panel.call.call_args_list]
        self.assertNotIn("/api/close-server", paths)
        self.assertEqual(paths[-1], "/api/cancel-shutdown")

    def test_network_restore_targets_only_existing_project_proxy(self):
        self.manager.device.adb.return_value = "127.0.0.1:17890"
        with patch.object(runtime, "port_open", return_value=True):
            self.manager.prepare_network()
        self.manager.device.adb.assert_any_call("reverse", "tcp:17890", "tcp:7890")
        self.manager.device.adb.reset_mock()
        self.manager.device.adb.return_value = "another-proxy:8888"
        self.manager.prepare_network()
        self.assertEqual(self.manager.device.adb.call_count, 1)

    def test_missing_network_dependency_is_explicit(self):
        self.manager.device.adb.return_value = "127.0.0.1:17890"
        with patch.object(runtime, "port_open", return_value=False), self.assertRaisesRegex(MotionError, "网络代理尚未启动"):
            self.manager.prepare_network()

    def test_map_ipv4_failure_keeps_network_configuration(self):
        self.manager.device.adb.return_value = "null"
        self.manager.map_connection_available.return_value = False
        with self.assertRaisesRegex(MotionError, "无法连接地图"):
            self.manager.prepare_network()
        self.assertFalse(any("sysctl" in call.args for call in self.manager.device.adb.call_args_list))

    def test_failed_ipv6_is_disabled_only_after_ipv4_succeeds(self):
        self.manager.map_connection_available.side_effect = (True, False, True)
        settings = {"all": "0", "default": "0"}
        def adb(*args, **kwargs):
            if args == ("shell", "settings", "get", "global", "http_proxy"):
                return "null"
            if args[:2] == ("shell", "cat"):
                return settings[args[2].split("/")[-2]]
            if args[:3] == ("shell", "sysctl", "-w"):
                key, value = args[3].split("=")
                settings[key.split(".")[-2]] = value
                return ""
            self.fail("Unexpected device command")
        self.manager.device.adb.side_effect = adb
        self.manager.prepare_network()
        self.assertEqual(settings, {"all": "1", "default": "1"})

    def test_map_repair_rolls_back_if_ipv4_stops_working(self):
        self.manager.map_connection_available.side_effect = (True, False, False)
        settings = {"all": "0", "default": "0"}
        def adb(*args, **kwargs):
            if args == ("shell", "settings", "get", "global", "http_proxy"):
                return "null"
            if args[:2] == ("shell", "cat"):
                return settings[args[2].split("/")[-2]]
            if args[:3] == ("shell", "sysctl", "-w"):
                key, value = args[3].split("=")
                settings[key.split(".")[-2]] = value
                return ""
            self.fail("Unexpected device command")
        self.manager.device.adb.side_effect = adb
        with self.assertRaisesRegex(MotionError, "地图服务连接检查"):
            self.manager.prepare_network()
        self.assertEqual(settings, {"all": "0", "default": "0"})

    def test_startup_location_sends_only_one_stationary_fix(self):
        with patch("emulator_route.load_route", return_value=([(121.5, 38.8)], [])), \
             patch("emulator_gps.CachedGps") as gps:
            self.manager.initialize_location()
        gps.return_value.update.assert_called_once()
        self.assertEqual(gps.return_value.update.call_args.args, ((121.5, 38.8), 0, 0))
        height = gps.return_value.update.call_args.kwargs["altitude_m"]
        self.assertTrue(75.9 <= height <= 82.5)
        gps.return_value.close.assert_called_once()

    def test_foreign_adb_executable_is_never_stopped(self):
        with patch.object(runtime.subprocess, "run", side_effect=[
            SimpleNamespace(stdout="p123\n"), SimpleNamespace(stdout="/other/project/adb\n", returncode=0)
        ]), self.assertRaisesRegex(MotionError, "其他程序"):
            self.manager.verify_adb_owner()
        self.manager.device.adb.assert_not_called()

    def test_shared_adb_with_other_device_is_never_stopped(self):
        expected = str((runtime.SDK / "platform-tools/adb").resolve()) + "\n"
        self.manager.device.adb.return_value = "List of devices attached\nemulator-15556\tdevice\nother-phone\tdevice\n"
        with patch.object(runtime.subprocess, "run", side_effect=[
            SimpleNamespace(stdout="p123\n"), SimpleNamespace(stdout=expected, returncode=0)
        ]), self.assertRaisesRegex(MotionError, "其他设备"):
            self.manager.verify_adb_owner()
        self.manager.device.adb.assert_called_once_with("devices")

    def test_known_live_baseline_is_reused_without_reapplying_mounts(self):
        binary = self.lab / "baseline"
        binary.write_bytes(b"fixture baseline")
        digest = runtime.hashlib.sha256(binary.read_bytes()).hexdigest()
        self.manager.device.read_boot.return_value = "fixture-boot"
        self.manager.device.adb.side_effect = lambda *args, **kwargs: "0" if args == ("shell", "id", "-u") else "Enforcing"
        self.manager.running_gnss = lambda: ("123", digest)
        with patch.object(runtime, "GNSS_BINARY", binary), patch.object(runtime, "GNSS_SHA256", digest):
            self.manager.prepare_location()
        self.assertFalse(any("mount" in call.args for call in self.manager.device.adb.call_args_list))
        self.assertTrue(json.loads(runtime.GNSS_STATE.read_text())["active"])

    def test_unknown_driver_is_rejected_before_root_or_mount(self):
        binary = self.lab / "baseline"
        binary.write_bytes(b"fixture baseline")
        digest = runtime.hashlib.sha256(binary.read_bytes()).hexdigest()
        self.manager.device.read_boot.return_value = "fixture-boot"
        self.manager.device.adb.side_effect = lambda *args, **kwargs: "0" if args == ("shell", "id", "-u") else "Enforcing"
        self.manager.running_gnss = lambda: ("123", "unknown")
        with patch.object(runtime, "GNSS_BINARY", binary), patch.object(runtime, "GNSS_SHA256", digest):
            with self.assertRaisesRegex(MotionError, "不是已知版本"):
                self.manager.prepare_location()
        self.assertFalse(any("root" in call.args or "mount" in call.args for call in self.manager.device.adb.call_args_list))

    def test_cold_boot_restores_debug_access_before_inspecting_driver(self):
        binary = self.lab / "baseline"
        binary.write_bytes(b"fixture baseline")
        digest = runtime.hashlib.sha256(binary.read_bytes()).hexdigest()
        operations = []
        ids = iter(("2000", "0"))
        def adb(*args, **kwargs):
            operations.append(args)
            return next(ids) if args == ("shell", "id", "-u") else "Enforcing"
        self.manager.device.adb.side_effect = adb
        self.manager.device.read_boot.return_value = "fixture-boot"
        def gnss():
            operations.append("inspect_driver")
            return "123", digest
        self.manager.running_gnss = gnss
        with patch.object(runtime, "GNSS_BINARY", binary), patch.object(runtime, "GNSS_SHA256", digest):
            self.manager.prepare_location()
        self.assertLess(operations.index(("root",)), operations.index("inspect_driver"))


class OwnershipTests(unittest.TestCase):
    def test_adb_missing_or_foreign_owner_cannot_autostart_or_send(self):
        for error in ("missing server", "foreign server"):
            with self.subTest(error=error), \
                 patch.object(session, "verify_adb_server", side_effect=MotionError(error)), \
                 patch.object(session.subprocess, "run") as execute, self.assertRaises(MotionError):
                session.Device().adb("shell", "id")
            execute.assert_not_called()

    def test_foreign_avd_prevents_direct_device_commands(self):
        with patch.object(session, "verify_adb_server"), \
             patch.object(session, "verify_emulator_owner", side_effect=MotionError("foreign AVD")), \
             patch.object(session.subprocess, "run") as execute, self.assertRaises(MotionError):
            session.Device().adb("shell", "input", "tap", "1", "1")
        execute.assert_not_called()

    def test_same_named_avd_requires_project_datadir(self):
        executable = str(motion.ROOT / "sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64")
        with patch.object(motion, "listener_pid", return_value=123), \
             patch.object(motion, "process_field", side_effect=[executable,
                 executable + " -avd sportworld_tablet34 -ports 15556,15557 -datadir /other/sportworld_tablet34.avd"]), \
             self.assertRaisesRegex(MotionError, "AVD 路径"):
            motion.verify_emulator_owner()

    def test_owned_avd_uses_exact_sdk_and_datadir(self):
        executable = str(motion.ROOT / "sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64")
        command = executable + " -avd sportworld_tablet34 -ports 15556,15557 -datadir " + str(motion.ROOT / "avd/sportworld_tablet34.avd")
        with patch.object(motion, "listener_pid", return_value=123), \
             patch.object(motion, "process_field", side_effect=[executable, command]):
            self.assertEqual(motion.verify_emulator_owner(), 123)

    def test_console_checks_owner_before_connection_or_authentication(self):
        with patch.object(motion, "verify_emulator_owner", side_effect=MotionError("foreign")), \
             patch.object(motion.socket, "create_connection") as connect, self.assertRaises(MotionError):
            motion.Console(15556)
        connect.assert_not_called()

    def test_path_viewer_does_not_override_its_bundled_server(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = Path(directory)
            executable = lab / "bin/scrcpy"
            executable.parent.mkdir()
            executable.touch()
            manager = runtime.Runtime(report=Mock())
            manager.device = Mock()
            with patch.object(runtime, "LAB", lab), \
                 patch.object(runtime, "SCRCPY", lab / "missing/scrcpy"), \
                 patch.object(runtime, "owned_pids", return_value=[]), \
                 patch.object(runtime.shutil, "which", return_value=str(executable)), \
                 patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as spawn:
                manager.open_viewer()
            self.assertNotIn("SCRCPY_SERVER_PATH", spawn.call_args.kwargs["env"])
            record = json.loads((lab / "android/viewer-process.json").read_text())
            self.assertEqual(record["executable"], str(executable.resolve()))


class DrainTests(unittest.TestCase):
    def test_draining_prevents_all_new_guest_commands(self):
        controller = control_panel.Controller()
        controller.device = Mock()
        controller.drain()
        for action in (lambda: controller.start("generate", {}), lambda: controller.answer({}),
                       controller.open_app, controller.open_viewer, controller.snapshot,
                       lambda: controller.interact({})):
            with self.assertRaisesRegex(MotionError, "正在彻底退出"):
                action()
        controller.device.adb.assert_not_called()
        controller.device.activity.assert_not_called()

    def test_shutdown_requests_are_single_use(self):
        controller = control_panel.Controller()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(control_panel, "ROOT", Path(directory)), \
             patch.object(control_panel.subprocess, "Popen") as process:
            (Path(directory) / ".lab").mkdir()
            controller.request_shutdown()
            controller.request_shutdown()
        self.assertEqual(process.call_count, 1)
        self.assertTrue(controller.closing)


if __name__ == "__main__":
    unittest.main()
