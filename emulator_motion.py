#!/usr/bin/env python3
"""Short, reversible synthetic sensor tests for the project's isolated AVDs.

No GPS, app interaction, authentication, network submission or step-counter writes.
Acceleration is varied along the existing gravity vector; sensor orientation is
not reconfigured. These waveforms are test inputs, not evidence of real exercise.
"""
import argparse
import math
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent / ".lab" / "android"
ALLOWED_AVDS = {15554: "sportworld_aosp34", 15556: "sportworld_tablet34"}
SENSORS = ("acceleration", "gyroscope")


class MotionError(RuntimeError):
    pass


def listener_pid(port):
    """Inspect local ownership without opening an adb or console connection."""
    try:
        result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise MotionError("无法核对本项目端口归属，请确认 macOS 的 lsof 可用。") from None
    pids = set(re.findall(r"^p(\d+)$", result.stdout, re.M))
    if len(pids) != 1:
        raise MotionError("本项目连接服务尚未就绪或端口归属不明确，请先完成 bootstrap 并启动测试平板。")
    return int(next(iter(pids)))


def process_field(pid, field):
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", field + "="],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise MotionError("无法核对本项目进程归属，已停止操作。") from None
    if result.returncode or not result.stdout.strip():
        raise MotionError("本项目进程已退出，已停止操作。")
    return result.stdout.strip()


def verify_adb_server():
    pid = listener_pid(15037)
    expected = (ROOT / "sdk/platform-tools/adb").resolve()
    if not expected.is_relative_to(ROOT.resolve()) or process_field(pid, "comm") != str(expected):
        raise MotionError("连接端口正在被其他程序使用，已保留该服务。请关闭其他环境后启动本项目。")
    return pid


def verify_emulator_owner(port=15556, avd="sportworld_tablet34"):
    if ALLOWED_AVDS.get(port) != avd:
        raise MotionError("模拟器端口或 AVD 不属于本项目配置，已停止操作。")
    pid = listener_pid(port)
    executable = Path(process_field(pid, "comm"))
    qemu = (ROOT / "sdk/emulator/qemu").resolve()
    datadir = (ROOT / "avd" / (avd + ".avd")).resolve()
    if (not qemu.is_relative_to(ROOT.resolve()) or not datadir.is_relative_to(ROOT.resolve())
            or not executable.is_relative_to(qemu) or not executable.name.startswith("qemu-system-")):
        raise MotionError("当前端口不是本项目的测试平板，已停止操作。")
    command = process_field(pid, "command")
    for flag, value in (("-avd", avd), ("-ports", f"{port},{port + 1}"), ("-datadir", str(datadir))):
        if not re.search(r"(?:^| )" + re.escape(flag) + " +" + re.escape(value) + r"(?: |$)", command):
            raise MotionError("测试平板的 AVD 路径与本项目不同，请通过本项目启动入口重新打开。")
    return pid


def checked_response(text):
    lines = text.replace("\r", "").splitlines()
    if any(line.startswith("KO") for line in lines):
        raise MotionError("Emulator rejected a command: " + text.strip())
    if "OK" not in lines:
        raise MotionError("Emulator response did not contain OK")
    return "\n".join(line for line in lines if line != "OK").strip()


class Console:
    def __init__(self, port):
        verify_emulator_owner(port, ALLOWED_AVDS.get(port))
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.settimeout(3)
        self.buffer = b""
        self.closed = False
        try:
            banner = self.response()
            if "Authentication required" in banner:
                match = re.search(r"^'([^'\r\n]+)'$", banner, re.M)
                if not match:
                    raise MotionError("Console did not advertise an auth path")
                path = Path(match.group(1)).resolve()
                if not path.is_relative_to(ROOT.resolve()):
                    # Only this task's explicitly prepared control credential may
                    # substitute for an external banner path. Never read that path.
                    path = (ROOT / "console-auth.token").resolve()
                if not path.is_relative_to(ROOT.resolve()):
                    raise MotionError("Isolated console credential resolves outside the lab")
                if not path.is_file():
                    raise MotionError("Isolated console credential is missing; no global fallback")
                if path.stat().st_size > 1024:
                    raise MotionError("Invalid console auth file size")
                token = path.read_text(encoding="ascii").strip()
                if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,1024}", token):
                    raise MotionError("Invalid console auth token format")
                # Never log the auth command or any response that could echo it.
                try:
                    self.command("auth " + token)
                except MotionError:
                    raise MotionError("Console authentication failed") from None
        except BaseException:
            self.socket.close()
            raise

    def response(self):
        lines = []
        while True:
            while b"\n" not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise MotionError("Console connection closed")
                self.buffer += chunk
                if len(self.buffer) > 65536:
                    raise MotionError("Oversized console response")
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.decode("utf-8", errors="replace").strip("\r")
            lines.append(line)
            if line == "OK" or line.startswith("KO"):
                return checked_response("\n".join(lines))

    def command(self, command):
        return self.commands((command,))[0]

    def commands(self, commands):
        """Pipeline validated commands, then consume each response in order."""
        if self.closed:
            raise MotionError("Console connection is closed")
        commands = tuple(commands)
        if any("\n" in command or "\r" in command for command in commands):
            raise MotionError("Multiline console commands are forbidden")
        payload = "".join(command+"\n" for command in commands).encode("ascii")
        if not commands:
            return []
        try:
            self.socket.sendall(payload)
            return [self.response() for _ in commands]
        except BaseException:
            # A partial write or unread acknowledgments leave framing ambiguous.
            # Recovery must use a fresh connection after any batch failure.
            try:
                self.close()
            except Exception:
                pass
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if hasattr(self, "cached_gps"):
                self.cached_gps.close()
        finally:
            self.socket.close()


def values(console, sensor):
    reply = console.command("sensor get " + sensor)
    match = re.search(r"^" + sensor + r"\s*=\s*([^\r\n]+)$", reply, re.M)
    if not match:
        raise MotionError("Cannot parse " + sensor + " response")
    parts = match.group(1).split(":")
    if len(parts) != 3:
        raise MotionError("Expected exactly three sensor axes")
    result = tuple(float(part) for part in parts)
    if not all(math.isfinite(value) for value in result):
        raise MotionError("Non-finite sensor state")
    return result


def set_values(console, sensor, vector):
    if not all(math.isfinite(value) for value in vector):
        raise MotionError("Refusing non-finite sensor input")
    console.command("sensor set " + sensor + " " + ":".join(format(v, ".12g") for v in vector))


def set_sensor_values(console, mapping):
    """Validate the complete sensor frame before pipelining its console writes."""
    from sensor_channels import COUPLED_SENSORS
    commands = []
    for sensor, vector in mapping.items():
        if sensor not in COUPLED_SENSORS:
            raise MotionError("Unknown sensor input")
        if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
            raise MotionError("Sensor input requires three finite axes")
        commands.append("sensor set " + sensor + " " + ":".join(format(value, ".12g") for value in vector))
    console.commands(commands)


def bounded(low, high):
    def parse(text):
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError("Expected a finite number") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise argparse.ArgumentTypeError(f"Expected a finite number in [{low}, {high}]")
        return value
    return parse


def restore(port, avd, original, identity_check=None):
    """Use a fresh connection: an interrupted command may leave an unread OK."""
    from sensor_channels import COUPLED_SENSORS
    if not set(SENSORS).issubset(original) or set(original) - set(COUPLED_SENSORS):
        raise MotionError("Restore requires acceleration/gyroscope and known sensor names")
    if any(len(vector) != 3 or not all(math.isfinite(value) for value in vector)
           for vector in original.values()):
        raise MotionError("Restore requires finite three-axis sensor baselines")
    sensors = tuple(sensor for sensor in COUPLED_SENSORS if sensor in original)
    recovery = Console(port)
    try:
        if identity_check is not None:
            identity_check()
        if recovery.command("avd name") != avd:
            raise MotionError("AVD changed; cannot safely restore a different device")
        errors = []
        for sensor in sensors:
            try:
                if identity_check is not None:
                    identity_check()
                set_values(recovery, sensor, original[sensor])
            except Exception as error:
                errors.append(f"{sensor}: {error}")
        if errors:
            raise MotionError("RESTORE FAILED: " + "; ".join(errors))
        restored = {}
        for sensor in sensors:
            if identity_check is not None:
                identity_check()
            restored[sensor] = values(recovery, sensor)
        if any(not all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
                       for a, b in zip(original[sensor], restored[sensor]))
               for sensor in sensors):
            raise MotionError("RESTORE VERIFICATION FAILED: " + str(restored))
        print("Restored and verified:", restored, flush=True)
    finally:
        recovery.close()


def run(args):
    if args.port not in ALLOWED_AVDS:
        raise MotionError("Only the project's fixed emulator ports are permitted")
    console = Console(args.port)
    try:
        avd = console.command("avd name")
        if avd != ALLOWED_AVDS[args.port]:
            raise MotionError("Refusing unexpected AVD identity: " + avd)
        original = {sensor: values(console, sensor) for sensor in SENSORS}
        gravity = math.sqrt(sum(v * v for v in original["acceleration"]))
        if gravity < 0.01:
            raise MotionError("Acceleration baseline has no usable gravity direction")
        direction = tuple(v / gravity for v in original["acceleration"])
        print(f"AVD={avd}; backend=persistent console; synthetic input only; requested Hz={args.rate:g}")
        print("Before:", original, flush=True)
        start = time.monotonic()
        ticks = 0
        stopped = False
        try:
            while (elapsed := time.monotonic() - start) < args.seconds:
                phase = elapsed * (args.cadence / 60) * 2 * math.pi
                # m/s² along the baseline vector; rad/s on the existing Z axis.
                acceleration = tuple(v + d * args.acceleration * math.sin(phase)
                                     for v, d in zip(original["acceleration"], direction))
                gyroscope = tuple(v + (args.gyroscope * math.sin(phase) if i == 2 else 0)
                                  for i, v in enumerate(original["gyroscope"]))
                set_values(console, "acceleration", acceleration)
                set_values(console, "gyroscope", gyroscope)
                ticks += 1
                # Do not queue a burst of overdue samples when the transport is slower.
                target = min(args.seconds, ticks / args.rate)
                delay = start + target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt:
            stopped = True
            print("Interrupted; restoring original sensors.", file=sys.stderr)
        finally:
            previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                # Never attempt protocol recovery on the interrupted stream.
                try:
                    console.close()
                except OSError:
                    pass
                try:
                    restore(args.port, avd, original)
                except Exception as error:
                    raise MotionError("RESTORE FAILED: " + str(error)) from error
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        duration = time.monotonic() - start
        print(f"Completed samples={ticks}; elapsed including restore={duration:.3f}s; "
              f"observed Hz including restore={ticks / duration:.2f}")
        return 130 if stopped else 0
    finally:
        console.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=bounded(0.1, 300), default=5.0)
    parser.add_argument("--cadence", type=bounded(80, 240), default=175.0,
                        help="Synthetic cycle frequency, cycles/minute; not measured steps")
    parser.add_argument("--rate", type=bounded(5, 60), default=20.0)
    parser.add_argument("--acceleration", type=bounded(0, 6), default=1.5,
                        help="Acceleration amplitude in m/s²")
    parser.add_argument("--gyroscope", type=bounded(0, 2), default=0.15,
                        help="Z-axis angular-velocity amplitude in rad/s")
    parser.add_argument("--port", type=int, choices=tuple(ALLOWED_AVDS), default=15556)
    args = parser.parse_args()
    try:
        from run_session import RunLock
        with RunLock():
            return run(args)
    except (MotionError, OSError, ValueError) as error:
        print("Sensor test failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
