"""Authenticated, loopback-only GPS updates through the official emulator API."""
import importlib
import math
from pathlib import Path
import struct
import sys
import time

from emulator_motion import MotionError, listener_pid, verify_emulator_owner

SERVICE = "/android.emulation.control.EmulatorController/"
DISCOVERY = Path.home() / "Library/Caches/TemporaryItems/avd/running"


def encode_fix(point, speed_knots, bearing, altitude_m=50.0):
    longitude, latitude = point
    fields = ((2, latitude), (3, longitude), (4, speed_knots * 1852 / 3600),
              (5, bearing % 360), (6, altitude_m))
    if not all(math.isfinite(value) for _, value in fields):
        raise MotionError("Non-finite cached GPS input")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90 or speed_knots < 0:
        raise MotionError("Cached GPS coordinate or speed outside valid range")
    # GpsState: passiveUpdate=true, fixed64 coordinates/speed/bearing/altitude,
    # satellites=6, matching the emulator hardware parser's actual SV count.
    return b"\x08\x01" + b"".join(bytes([(key << 3) | 1]) + struct.pack("<d", value)
                                  for key, value in fields) + b"\x38\x06"


def discover(avd, console_port):
    pid = verify_emulator_owner(console_port, avd)
    path = DISCOVERY / f"pid_{pid}.ini"
    if not path.is_file() or path.resolve().parent != DISCOVERY.resolve():
        raise MotionError("需要先启动此测试平板的官方 GPS 接口")
    entry = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    if entry.get("avd.name") != avd or entry.get("port.serial") != str(console_port):
        raise MotionError("官方 GPS 接口与本项目测试平板身份不一致")
    port = entry.get("grpc.port", "")
    if not port.isdecimal() or not 1024 <= int(port) <= 65535 or not entry.get("grpc.token"):
        raise MotionError("测试平板 GPS 接口的认证配置不完整")
    if listener_pid(int(port)) != pid:
        raise MotionError("官方 GPS 接口端口不属于本项目测试平板")
    return int(port), entry["grpc.token"]


def grpc_module():
    try:
        return importlib.import_module("grpc")
    except ImportError:
        site = Path(__file__).resolve().parent / ".lab/gps-env/lib" / (
            f"python{sys.version_info.major}.{sys.version_info.minor}") / "site-packages"
        if not site.is_dir():
            raise MotionError("本机 GPS 接口运行环境尚未准备") from None
        sys.path.insert(0, str(site))
        return importlib.import_module("grpc")


class CachedGps:
    def __init__(self, avd, console_port):
        port, token = discover(avd, console_port)
        self.grpc = grpc_module()
        self.channel = self.grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            self.grpc.channel_ready_future(self.channel).result(timeout=3)
        except self.grpc.FutureTimeoutError:
            self.channel.close()
            raise MotionError("测试平板 GPS 接口连接超时") from None
        self.metadata = (("authorization", "Bearer " + token),)
        self.send = self.channel.unary_unary(SERVICE + "setGps")

    def update(self, point, speed_knots, bearing, altitude_m=50.0):
        payload = encode_fix(point, speed_knots, bearing, altitude_m)
        deadline = time.monotonic()+.20
        timeout = .15
        for attempt in range(2):
            try:
                self.send(payload, metadata=self.metadata, timeout=timeout)
                return
            except self.grpc.RpcError as error:
                status = error.code()
                remaining = deadline-time.monotonic()
                if (attempt == 1 or remaining <= 0 or status not in
                        (self.grpc.StatusCode.DEADLINE_EXCEEDED, self.grpc.StatusCode.UNAVAILABLE)):
                    raise MotionError("官方 GPS 接口更新失败：" + status.name) from None
                # An acknowledgment timeout may follow a successful state update.
                # Retry the identical state once, without claiming exactly-once delivery.
                timeout = min(.05, remaining)

    def close(self):
        self.channel.close()
