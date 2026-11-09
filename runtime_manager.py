"""Start and stop only this project's local tablet and control panel."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import uuid

from emulator_motion import Console, MotionError
from run_session import Device, GNSS_SHA256, PACKAGE, RunLock, atomic_json, now, verify_adb_server

ROOT = Path(__file__).resolve().parent
LAB = ROOT / ".lab"
SDK = LAB / "android/sdk"
AVD = "sportworld_tablet34"
SERIAL = "emulator-15556"
PORT = 15556
ADB_PORT = 15037
GNSS_TARGET = "/vendor/bin/hw/android.hardware.gnss-service.ranchu"
GNSS_SERVICE = "vendor.gnss.ranchu"
GNSS_ORIGINAL = "ea1ec67a7e1d4e8407b7162d49929477e142cdaf815ce59d01cb34480a10b93c"
GNSS_BINARY = LAB / "gnss-analysis/gnss-used-in-fix-quality1-test"
GNSS_STATE = LAB / "gnss-analysis/applied-state.json"
RUNTIME_STATE = LAB / "runtime-status.json"
SCRCPY = LAB / "scrcpy/scrcpy-macos-aarch64-v5.0.1/scrcpy"
ACTIVE = {"running", "waiting", "stopping"}


def wait_until(check, seconds, message):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise MotionError(message)
        time.sleep(.25)


@contextmanager
def lifecycle_lock(*, wait=False):
    LAB.mkdir(parents=True, exist_ok=True)
    with (LAB / "runtime.lock").open("a+") as stream:
        os.chmod(stream.name, 0o600)
        def acquire():
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        if wait:
            wait_until(acquire, 300, "启动或退出程序仍未完成，请保留窗口。")
        elif not acquire():
            raise MotionError("正在启动或退出，请等当前窗口完成后再点击。")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def process_list():
    result = subprocess.run(["ps", "-ww", "-axo", "pid=,comm="], capture_output=True, text=True, check=True)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            rows.append((int(fields[0]), fields[1]))
    return rows


def owned_pids(kind):
    """Match the executable AND this tablet; never use a global process-name kill."""
    result = []
    emulator_dir = str(SDK / "emulator/qemu") + "/"
    try:
        viewer = json.loads((LAB / "android/viewer-process.json").read_text())
    except (OSError, ValueError):
        viewer = {}
    for pid, executable in process_list():
        if kind == "viewer":
            candidate = viewer.get("pid") == pid and (executable == str(SCRCPY)
                         or (viewer.get("project") == str(ROOT.resolve())
                             and executable == viewer.get("executable")))
        elif kind == "emulator":
            candidate = executable.startswith(emulator_dir) and Path(executable).name.startswith("qemu-system-")
        else:
            raise ValueError("Unknown process kind")
        if not candidate:
            continue
        command = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.strip()
        if kind == "viewer":
            matches = re.search(r"(?:^| )--serial(?:=| )" + re.escape(SERIAL) + r"(?: |$)", command)
        else:
            matches = re.search(r"(?:^| )-avd " + re.escape(AVD) + r"(?: |$)", command)
            matches = matches and re.search(r"(?:^| )-ports 15556,15557(?: |$)", command)
            matches = matches and re.search(r"(?:^| )-datadir " + re.escape(str((LAB / "android/avd" / (AVD + ".avd")).resolve())) + r"(?: |$)", command)
        if matches:
            result.append(pid)
    return result


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.3):
            return True
    except OSError:
        return False


class Panel:
    def __init__(self, state):
        self.state = state
        self.base = "http://127.0.0.1:" + str(int(state["port"]))

    def call(self, path, payload=None):
        headers = {"Cookie": "campus_control=" + self.state["token"]}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers.update({"Origin": self.base, "Content-Type": "application/json"})
        with urlopen(Request(self.base + path, data=data, headers=headers), timeout=5) as response:
            return json.load(response)

    @classmethod
    def current(cls):
        try:
            panel = cls(json.loads((LAB / "control-panel.json").read_text()))
            health = panel.call("/health")
            if health.get("service") == "campus-control" and health.get("instance") == panel.state["instance"]:
                return panel
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None


class Runtime:
    def __init__(self, report=print):
        self.device = Device()
        self.report = report

    def record(self, phase, **values):
        atomic_json(RUNTIME_STATE, {"phase": phase, "updated_at": now().isoformat(), **values})

    def connected(self):
        try:
            return self.device.adb("get-state", timeout=2) == "device"
        except MotionError:
            return False

    def booted(self):
        try:
            return self.device.adb("shell", "getprop", "sys.boot_completed", timeout=2) == "1"
        except MotionError:
            return False

    def identify(self):
        console = Console(PORT)
        try:
            if console.command("avd name") != AVD:
                raise MotionError("当前端口不是本项目的测试平板，已停止操作。")
        finally:
            console.close()

    def launch_guest(self):
        if self.connected():
            self.identify()
            return
        if not owned_pids("emulator"):
            if port_open(PORT) or port_open(PORT + 1):
                raise MotionError("平板端口被其他程序占用，请保留错误窗口。")
            self.report("正在启动测试平板，首次打开需要稍等……", flush=True)
            starter = ROOT / "scripts/start_android_tablet.sh"
            if (not starter.is_file() or not (LAB / "android-env.sh").is_file()
                    or not (SDK / "platform-tools/adb").is_file()
                    or not (SDK / "emulator/emulator").is_file()
                    or not (LAB / "android/avd" / (AVD + ".avd/config.ini")).is_file()):
                raise MotionError("测试平板尚未配置，请先运行 python3 scripts/bootstrap.py --android，再重新启动。")
            logs = LAB / "android/logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "tablet-start.log").open("ab") as log:
                subprocess.Popen(["/bin/sh", str(starter), "--window"], cwd=ROOT,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_until(self.connected, 120, "测试平板连接超时，请查看 .lab/android/logs/tablet-start.log，确认端口空闲并已运行 python3 scripts/bootstrap.py --android。")
        self.identify()

    def running_gnss(self):
        pids = self.device.adb("shell", "pidof", "android.hardware.gnss-service.ranchu").split()
        if len(pids) != 1 or not pids[0].isdigit():
            raise MotionError("定位服务尚未就绪。")
        digest = self.device.adb("shell", "sha256sum", f"/proc/{pids[0]}/exe").split()[0]
        return pids[0], digest

    def prepare_location(self):
        """Reapply the verified temporary baseline; never overwrite the vendor image."""
        if not GNSS_BINARY.is_file():
            raise MotionError("定位组件尚未准备，请按 README 提取干净 AVD 的原始 GNSS 文件，再运行 python3 scripts/patch_gnss.py 原始文件路径。")
        if hashlib.sha256(GNSS_BINARY.read_bytes()).hexdigest() != GNSS_SHA256:
            raise MotionError("本地定位组件与已验证版本不同，已停止启动。")
        boot = self.device.read_boot()
        self.device.boot_id = boot
        if self.device.adb("shell", "getenforce") != "Enforcing":
            raise MotionError("平板安全状态与已验证环境不同，已停止启动。")
        # A cold boot resets adbd to the shell user, which cannot inspect /proc/*/exe.
        # This is the project's debuggable AVD; obtain its existing debug access first.
        if self.device.adb("shell", "id", "-u") != "0":
            self.device.adb("root", timeout=10)
            def rooted():
                try:
                    return self.device.adb("shell", "id", "-u", timeout=2) == "0"
                except MotionError:
                    return False
            wait_until(rooted, 20, "无法准备本项目的测试平板。")
            self.device.verify_boot()
        pid, digest = self.running_gnss()
        if digest != GNSS_SHA256:
            if digest != GNSS_ORIGINAL:
                raise MotionError("定位服务不是已知版本，已停止启动。")
            self.report("正在恢复重启后所需的环境配置……", flush=True)
            self.device.verify_boot()
            namespaces = self.device.adb("shell", "readlink", "/proc/1/ns/mnt", "/proc/self/ns/mnt").splitlines()
            if len(namespaces) != 2 or namespaces[0] != namespaces[1]:
                raise MotionError("平板环境隔离状态异常，已停止启动。")
            if self.device.adb("shell", "sha256sum", GNSS_TARGET).split()[0] != GNSS_ORIGINAL:
                raise MotionError("定位组件文件状态异常，已停止启动。")
            context = self.device.adb("shell", "ls", "-Zd", GNSS_TARGET).split()[0]
            if not re.fullmatch(r"u:object_r:[a-z_0-9]+:s0", context):
                raise MotionError("定位组件权限状态异常，已停止启动。")
            stage = "/data/local/tmp/gnss-private-" + uuid.uuid4().hex[:8]
            guest = stage + "/gnss"
            self.record("preparing_location", boot_id=boot)
            self.device.adb("shell", "mkdir", "-p", stage)
            self.device.adb("shell", "mount", "-t", "tmpfs", "-o", "size=4m,mode=755,nodev", "tmpfs", stage)
            self.device.adb("push", str(GNSS_BINARY), guest, timeout=15)
            self.device.adb("shell", "chmod", "755", guest)
            self.device.adb("shell", "chcon", context, guest)
            self.device.adb("shell", "mount", "--bind", guest, GNSS_TARGET)
            self.device.adb("shell", "setprop", "ctl.restart", GNSS_SERVICE)
            def verified():
                try:
                    current_pid, current_digest = self.running_gnss()
                    return current_pid if current_digest == GNSS_SHA256 else None
                except MotionError:
                    return None
            pid = wait_until(verified, 20, "定位环境恢复失败，请保留错误窗口。")
            # Keep the platform default used by the successful acceptance run.
            self.device.adb("shell", "am", "compat", "reset", "144027538", PACKAGE, timeout=8)
            state = {"private_stage": stage, "private_binary": guest, "namespace": namespaces[0]}
        else:
            try:
                previous = json.loads(GNSS_STATE.read_text())
            except (OSError, ValueError):
                previous = {}
            state = {key: previous[key] for key in ("private_stage", "private_binary", "namespace")
                     if key in previous and previous.get("boot_id") == boot}
        self.device.verify_boot()
        if self.device.adb("shell", "getenforce") != "Enforcing":
            raise MotionError("环境恢复后的安全状态检查未通过。")
        atomic_json(GNSS_STATE, {**state, "boot_id": boot, "current_pid": pid, "target": GNSS_TARGET,
                               "service": GNSS_SERVICE, "active": True, "status": "baseline_verified_by_launcher",
                               "active_variant": "quality1_cn0_30", "vendor_original_overwritten": False,
                               "running_executable_sha256_verified": GNSS_SHA256, "selinux": "Enforcing",
                               "reinitialized_at": now().isoformat()})

    def prepare_network(self):
        proxy = self.device.adb("shell", "settings", "get", "global", "http_proxy")
        if proxy == "127.0.0.1:17890":
            self.device.adb("reverse", "tcp:17890", "tcp:7890")
            if not port_open(7890):
                raise MotionError("电脑上的网络代理尚未启动。请先打开原来使用的代理，再重新点击启动。")
        if not self.map_connection_available("-4"):
            raise MotionError("平板暂时无法连接地图服务，请稍后重新打开控制台。")
        if self.map_connection_available("-6"):
            return
        # Some hosts advertise an IPv6 route which cannot reach the map CDN.
        # Keep this repair inside the guest and verify IPv4 before changing it.
        self.report("正在修复平板的地图联网配置……", flush=True)
        original = {}
        for scope in ("all", "default"):
            value = self.device.adb("shell", "cat", f"/proc/sys/net/ipv6/conf/{scope}/disable_ipv6")
            if value not in ("0", "1"):
                raise MotionError("无法确认平板网络配置，已停止启动。")
            original[scope] = value
        try:
            for scope in original:
                self.device.adb("shell", "sysctl", "-w", f"net.ipv6.conf.{scope}.disable_ipv6=1")
            for scope in original:
                value = self.device.adb("shell", "cat", f"/proc/sys/net/ipv6/conf/{scope}/disable_ipv6")
                if value != "1":
                    raise MotionError("平板网络配置恢复失败，请保留提示窗口。")
            if not self.map_connection_available("-4"):
                raise MotionError("地图服务连接检查未通过，请保留提示窗口。")
        except Exception:
            for scope, value in original.items():
                try:
                    self.device.adb("shell", "sysctl", "-w", f"net.ipv6.conf.{scope}.disable_ipv6={value}")
                except MotionError:
                    pass
            raise

    def map_connection_available(self, family):
        if family not in ("-4", "-6"):
            raise ValueError("Invalid address family")
        command = f"toybox nc {family} -w 4 -W 1 -q 1 dualstack-arestapi.amap.com 443 </dev/null"
        try:
            self.device.adb("shell", command, timeout=6)
            return True
        except MotionError:
            return False

    def initialize_location(self):
        """Set only the calibrated starting position, at zero speed."""
        from emulator_route import load_route
        from emulator_gps import CachedGps
        from elevation_profile import ElevationProfile
        vertices, _ = load_route(ROOT / ".lab/routes/neusoft-track-route.json")
        self.device.verify_boot()
        connection = CachedGps(AVD, PORT)
        try:
            connection.update(vertices[0], 0, 0, altitude_m=ElevationProfile().at(0, 0)["altitude_m"])
        finally:
            connection.close()

    def prepare_static_sensors(self):
        """Make the idle Android degree-based orientation agree with its vectors."""
        from emulator_motion import restore, set_values, values
        from sensor_channels import capture_baseline, android_orientation
        connection = Console(PORT)
        try:
            if connection.command("avd name") != AVD:
                raise MotionError("传感器连接不是本项目平板，已停止准备。")
            self.device.verify_boot()
            baseline = capture_baseline(connection)
            if any(abs(value) > .1 for name in ("gyroscope", "gyroscope-uncalibrated") for value in baseline[name]):
                raise MotionError("平板传感器仍在运动，请先结束其他输入流程。")
            stationary = {**baseline,
                          "acceleration-uncalibrated": baseline["acceleration"],
                          "gyroscope-uncalibrated": baseline["gyroscope"],
                          "magnetic-field-uncalibrated": baseline["magnetic-field"],
                          "orientation": android_orientation(baseline["acceleration"], baseline["magnetic-field"])}
            try:
                for name, vector in stationary.items():
                    self.device.verify_boot()
                    set_values(connection, name, vector)
                for name, vector in stationary.items():
                    actual = values(connection, name)
                    if any(not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(actual, vector)):
                        raise MotionError("平板静止传感器准备后回读不一致。")
            except BaseException:
                restore(PORT, AVD, baseline, identity_check=self.device.verify_boot)
                raise
        finally:
            connection.close()

    def verify_adb_owner(self):
        verify_adb_server()
        devices = self.device.adb("devices").splitlines()[1:]
        if any(line.split()[0] != SERIAL for line in devices if line.strip()):
            raise MotionError("连接服务还被其他设备使用，已保留该服务，请先断开其他设备。")

    def open_viewer(self):
        if owned_pids("viewer"):
            return
        executable = str(SCRCPY) if SCRCPY.is_file() else shutil.which("scrcpy")
        if not executable:
            raise MotionError("scrcpy 尚未安装。可直接使用已打开的 Android 模拟器窗口，或安装 scrcpy 后重试此按钮。")
        self.device.adb("shell", "wm", "size", "1280x640")
        self.device.adb("shell", "wm", "density", "160")
        self.device.adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        self.device.adb("shell", "wm", "user-rotation", "lock", "1")
        env = {key: value for key, value in os.environ.items()
               if key not in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "SCRCPY_SERVER_PATH")}
        env.update({"ADB": str(SDK / "platform-tools/adb"), "ANDROID_ADB_SERVER_PORT": str(ADB_PORT)})
        server = Path(executable).parent / "scrcpy-server"
        if server.is_file():
            env["SCRCPY_SERVER_PATH"] = str(server)
        (LAB / "android/logs").mkdir(parents=True, exist_ok=True)
        with (LAB / "android/logs/viewer-open.log").open("ab") as log:
            child = subprocess.Popen([executable, "--serial=" + SERIAL, "--no-audio", "--no-clipboard-autosync",
                                      "--window-title=校园跑测试平板", "--window-width=520", "--window-height=832",
                                      "--max-size=1280", "--max-fps=30", "--video-bit-rate=2M"],
                                     cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        atomic_json(LAB / "android/viewer-process.json", {"pid": child.pid, "serial": SERIAL,
                                                        "project": str(ROOT.resolve()), "executable": str(Path(executable).resolve()),
                                                        "recording_enabled": False, "clipboard_autosync": False})

    def start(self, *, browser=True, viewer=False):
        with lifecycle_lock():
            self.record("starting")
            panel = Panel.current()
            panel_status = panel.call("/api/status") if panel else {}
            if panel_status.get("closing"):
                raise MotionError("正在彻底退出，请等待完成后再打开。")
            job = panel_status.get("job") or {}
            if job.get("state") not in ACTIVE:
                with RunLock():
                    if port_open(ADB_PORT):
                        self.verify_adb_owner()
                    self.launch_guest()
                    wait_until(self.booted, 120, "测试平板启动超时，请保留错误窗口。")
                    if not self.device.adb("shell", "pm", "path", PACKAGE).startswith("package:"):
                        raise MotionError("测试平板已打开。请在模拟器窗口手动安装官方运动世界 APK 并登录，然后重新启动控制台。")
                    self.prepare_location()
                    self.prepare_network()
                    self.device.adb("shell", "settings", "put", "secure", "location_mode", "3")
                    self.device.adb("emu", "grpc", str(PORT + 3000))
                    from emulator_gps import discover
                    discover(AVD, PORT)
                    self.initialize_location()
                    self.prepare_static_sensors()
                    if PACKAGE not in self.device.activity():
                        self.device.adb("shell", "am", "start", "-n", PACKAGE + "/.mvi.splash.SplashActivity", timeout=10)
            from control_panel import launch
            launch(open_browser=browser)
            if viewer:
                self.open_viewer()
            self.record("ready", guest_running=True, panel_running=True)
            self.report("已打开。下次仍双击这个入口即可，无需重新配置。", flush=True)

    def stop(self):
        with lifecycle_lock(wait=True):
            panel = Panel.current()
            self.record("stopping")
            self.report("正在停止输入并等待恢复……", flush=True)
            try:
                if panel:
                    panel.call("/api/drain", {})
                    wait_until(lambda: (panel.call("/api/status").get("job") or {}).get("state") not in ACTIVE,
                               35, "输入尚未停止，已保留平板和控制台，请稍后重试退出。")
                with RunLock():
                    if port_open(ADB_PORT):
                        self.verify_adb_owner()
                    for pid in owned_pids("viewer"):
                        if pid in owned_pids("viewer"):
                            os.kill(pid, signal.SIGTERM)
                    wait_until(lambda: not owned_pids("viewer"), 10, "平板窗口尚未退出，请重试。")
                    if self.connected():
                        self.identify()
                        self.report("正在关闭测试平板……", flush=True)
                        self.device.adb("emu", "kill", timeout=10)
                    else:
                        # The SDK handles SIGTERM as an orderly emulator exit.
                        for pid in owned_pids("emulator"):
                            if pid in owned_pids("emulator"):
                                os.kill(pid, signal.SIGTERM)
                    wait_until(lambda: not owned_pids("emulator"), 30, "测试平板尚未退出，控制台已保留，请重试。")
                    wait_until(lambda: not port_open(PORT) and not port_open(PORT + 1),
                               15, "平板端口尚未释放，控制台已保留，请重试。")
                    if port_open(ADB_PORT):
                        self.verify_adb_owner()
                        self.device.adb("kill-server", timeout=8)
                    wait_until(lambda: not port_open(ADB_PORT), 8, "本项目的连接服务尚未退出，请重试。")
                    if GNSS_STATE.exists():
                        state = json.loads(GNSS_STATE.read_text())
                        atomic_json(GNSS_STATE, {**state, "active": False, "status": "tablet_closed"})
                    self.record("stopped", guest_running=False, panel_running=False)
                    if panel:
                        panel.call("/api/close-server", {})
                        wait_until(lambda: not port_open(int(panel.state["port"])), 10, "控制台尚未退出，请重试。")
                self.report("已彻底退出：网页服务、测试平板、平板窗口和本项目的连接服务均已关闭。登录和已保存记录保留。", flush=True)
            except Exception:
                if panel:
                    try:
                        panel.call("/api/cancel-shutdown", {})
                    except (OSError, ValueError):
                        pass
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--viewer", action="store_true")
    args = parser.parse_args()
    runtime = Runtime()
    try:
        if args.action == "start":
            runtime.start(browser=not args.no_browser, viewer=args.viewer)
        else:
            runtime.stop()
    except (MotionError, OSError, ValueError, KeyError) as error:
        # Never include request objects, cookies, URLs or external command output.
        message = str(error) if isinstance(error, MotionError) else "操作未完成，请保留当前窗口并重试。"
        runtime.record("failed", error=message)
        if args.action == "stop":
            panel = Panel.current()
            if panel:
                try:
                    panel.call("/api/cancel-shutdown", {})
                except (OSError, ValueError):
                    pass
        print(message, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
