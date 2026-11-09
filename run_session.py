"""Bounded, manually coordinated runs in the single isolated Android AVD."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from emulator_motion import (Console, MotionError, restore, set_sensor_values, values,
                             verify_adb_server, verify_emulator_owner)
from emulator_route import AVD, PORT, gps, identify, metres
from motion_reference import ROOT
from run_trajectory import HZ, build_path, build_samples, verify_samples
from ui_protocol import ask, choose, acknowledge, web_active

PRIVATE = ROOT / "private"
RUNS = PRIVATE / "runs"
PACKAGE = "com.zjwh.android_wh_physicalfitness"
GNSS_SHA256 = "4c77d8efbcab5ee685c3e40fa6553f93e0d1317698d2795c79b79465e98c2c41"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(TIMEZONE)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RunLock:
    def __init__(self, path=None):
        self.path = path or PRIVATE / "input.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise MotionError("已有输入程序运行，请先结束另一个窗口") from None
        return self

    def __exit__(self, *args):
        fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()


def validate_mode(mode):
    if mode not in ("campus", "outdoor"):
        raise MotionError("跑步模式应为 campus 或 outdoor")
    return mode


def running_activity_matches(activity, mode):
    validate_mode(mode)
    name = "OutdoorRunActivity" if mode == "outdoor" else "RunRandomActivity"
    component = re.escape(PACKAGE) + r"/(?:\.mvi\.run\.|" + re.escape(PACKAGE) + r"\.mvi\.run\.)" + name
    return re.search(component + r"(?=[\s}]|$)", activity) is not None



def require_app_idle(device, *, home=False):
    """Read the official foreground activity before starting or resolving a run."""
    activity = device.activity()
    if any(running_activity_matches(activity, mode) for mode in ("campus", "outdoor")):
        raise MotionError("App 中还有进行中的跑步，请先手动暂停并结束本次跑步；同次续跑请使用续跑入口。")
    component = re.escape(PACKAGE) + r"/(?:\.|" + re.escape(PACKAGE) + r"\.)"
    if re.search(component + r"[A-Za-z0-9_.$]+(?=[\s}]|$)", activity) is None:
        raise MotionError("无法确认 App 当前页面，请打开运动世界并核对本次跑步是否已结束。")
    if home and re.search(component + r"mvi\.home\.HomeActivity(?=[\s}]|$)", activity) is None:
        raise MotionError("请先结束 App 中的本次跑步并返回首页，再创建新的输入。")
    return activity


def optional_outdoor_result(state):
    """Completed, restored outdoor inputs cannot have campus credit to resolve."""
    segments = state.get("segments") or []
    return (state.get("mode") == "outdoor" and state.get("state") == "review_pending"
            and bool(segments) and all(s.get("restoration_verified") is True for s in segments))


def preparation_cancellable(state):
    """Only offer explicit operator cancellation before any recorded run input."""
    stopped = state.get("state") == "preparation_stopped"
    legacy_stop = state.get("state") == "failed" and state.get("error") == "手动中止"
    return ((stopped or legacy_stop) and state.get("segments") == []
            and not state.get("first_sync_confirmed_at")
            and not state.get("result"))


def check_daily_credit(today=None, private=None, mode="campus"):
    validate_mode(mode)
    private = private or PRIVATE
    today = today or now().date().isoformat()
    legacy = private / "session-status.json"
    if legacy.exists():
        state = json.loads(legacy.read_text())
        accepted = state.get("acceptance", {})
        date = accepted.get("record_display_date", "").replace("/", "-")
        if date:
            try:
                date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
            except ValueError as error:
                raise MotionError("历史成绩日期无效，请核对记录") from error
        if mode == "campus" and state.get("valid_credit_observed") and date == today:
            raise MotionError("今天已有一条计分记录。每天最多计一次，程序已停止")
    for path in sorted((private / "runs").glob("*.json")):
        state = json.loads(path.read_text())
        if optional_outdoor_result(state):
            continue
        if mode == "campus" and (state.get("result") or {}).get("credited") is True and state.get("date") == today:
            raise MotionError("今天已有已登记的计分记录，程序已停止")
        if state.get("state") in ("armed", "running", "paused", "review_pending", "failed", "preparation_stopped"):
            raise MotionError(f"有一条运行记录待核验。请先用 --record-result 核对 {path.name}")


def review_path(route_path):
    return route_path.with_suffix(route_path.suffix + ".review.json")


def validate_route_review(route_path, data, today=None):
    path = review_path(route_path)
    if not path.exists():
        raise MotionError("缺少当天点位核对记录，请先运行 --review-route")
    review = json.loads(path.read_text())
    today = today or now().date().isoformat()
    if review.get("date") != today or review.get("route_sha256") != hashlib.sha256(route_path.read_bytes()).hexdigest():
        raise MotionError("点位核对已过期或路线已修改，请重新核对")
    if review.get("confirmed_course") is not True or review.get("method") not in ("user_checked_app_map", "agent_checked_app_map"):
        raise MotionError("路线缺少人工核对标记")
    if review["method"] == "agent_checked_app_map":
        evidence = Path(review.get("evidence_file", "")).resolve()
        if (evidence.parent != PRIVATE.resolve() or not evidence.is_file()
                or hashlib.sha256(evidence.read_bytes()).hexdigest() != review.get("evidence_sha256")):
            raise MotionError("自动核对缺少可复查的 App 地图证据")
    markers = data.get("checkpoint_markers")
    if not isinstance(markers, dict) or not 3 <= len(markers) <= 20:
        raise MotionError("路线需要包含实际点位坐标")
    if set(review.get("checkpoint_keys", [])) != set(markers):
        raise MotionError("核对的点位集合与当前路线不一致")
    all_positions = review.get("review_scope") == "all_reference_checkpoint_positions"
    if not all_positions and (not review.get("mandatory_key") or review["mandatory_key"] not in markers):
        raise MotionError("缺少已确认的必经点")
    route = build_path(data)
    distances = {}
    for label, point in markers.items():
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in point):
            raise MotionError("点位坐标无效")
        # Geometry is diagnostic only; no observed App rule defines a hit radius.
        closest = min(metres(tuple(point), route.coordinates(candidate)) for candidate in route.points)
        distances[label] = round(closest, 2)
    return {**review, "checkpoint_distances_m": distances, "app_checkpoint_acceptance": "unverified"}


def review_route(route_path, data):
    markers = data.get("checkpoint_markers", {})
    if not 3 <= len(markers) <= 20:
        raise MotionError("该路线缺少实际点位坐标，需要先根据 App 地图校准")
    source = data.get("checkpoint_source", "")
    observed_date = re.search(r"\d{4}-\d{2}-\d{2}", source)
    source_label = observed_date.group() if observed_date else "日期未核实"
    description = (f"本地参照来自 {source_label}，不是已确认的今日点位。请对照右侧 App 地图核对全部点位的位置和跑道。"
                   "必经点由 App 指定，控制台无需再次选择；位置一致时继续。")
    response = choose(description+"\n一致请输入 已核对，不一致请输入 停止\n",
                      [("地图一致，继续", "已核对"), ("地图不同，停止准备", "停止")],
                      text=description).strip()
    if response != "已核对":
        raise MotionError("点位核对未完成")
    review = {"schema": 2, "date": now().date().isoformat(), "checked_at": now().isoformat(),
              "route_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
              "checkpoint_keys": list(markers), "mandatory_key": None,
              "review_scope": "all_reference_checkpoint_positions",
              "confirmed_course": True, "method": "user_checked_app_map",
              "checkpoint_reference_source": source, "app_checkpoint_acceptance": "unverified"}
    atomic_json(review_path(route_path), review)
    try:
        validate_route_review(route_path, data)
    except Exception:
        review_path(route_path).unlink(missing_ok=True)
        raise
    print("地图核对已保存；本地几何距离仅供参照，实际打卡状态以 App 为准。")


class Device:
    def __init__(self):
        self.prefix = [str(ROOT / ".lab/android/sdk/platform-tools/adb"), "-P", "15037", "-s", "emulator-15556"]
        self.boot_id = None

    def adb(self, *args, timeout=3):
        verify_adb_server()
        if args not in (("devices",), ("kill-server",)):
            verify_emulator_owner(PORT, AVD)
        try:
            # All our pipes/sockets are non-inheritable. Allow posix_spawn so the
            # live gRPC threads do not enter fork handlers on every boot check.
            env = {key: value for key, value in os.environ.items()
                   if key not in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS")}
            env["ANDROID_ADB_SERVER_PORT"] = "15037"
            result = subprocess.run(self.prefix + list(args), capture_output=True, text=True,
                                    timeout=timeout, close_fds=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MotionError("测试平板连接中断或响应超时") from error
        if result.returncode:
            raise MotionError("测试平板命令失败，请检查连接")
        return result.stdout.strip()

    def read_boot(self):
        value = self.adb("shell", "cat", "/proc/sys/kernel/random/boot_id", timeout=2)
        if not re.fullmatch(r"[0-9a-f-]{36}", value):
            raise MotionError("无法确认测试平板启动身份")
        return value

    def verify_boot(self):
        if self.boot_id is None or self.read_boot() != self.boot_id:
            raise MotionError("测试平板已重启，输入停止，恢复操作已拒绝")

    def activity(self):
        output = self.adb("shell", "dumpsys", "activity", "activities")
        candidates = [line for line in output.splitlines() if "mResumedActivity:" in line or "topResumedActivity=" in line]
        return "\n".join(candidates)

    def preflight(self):
        if self.adb("get-state") != "device":
            raise MotionError("测试平板未连接")
        self.boot_id = self.read_boot()
        if self.adb("shell", "getprop", "sys.boot_completed") != "1":
            raise MotionError("测试平板尚未完成启动")
        if self.adb("shell", "settings", "get", "secure", "location_mode") in ("0", "null", ""):
            raise MotionError("测试平板的定位功能未开启")
        if self.adb("shell", "getenforce") != "Enforcing":
            raise MotionError("测试平板安全状态与已验证环境不符")
        activity = self.activity()
        if PACKAGE not in activity or not any(name in activity for name in ("HomeActivity", "RunRandomActivity", "OutdoorRunActivity")):
            raise MotionError("请先在运动世界中登录并打开首页或跑步页面")
        pids = self.adb("shell", "pidof", "android.hardware.gnss-service.ranchu").split()
        if len(pids) != 1 or not pids[0].isdigit():
            raise MotionError("定位服务状态异常，需要重新准备测试环境")
        actual = self.adb("shell", "sha256sum", f"/proc/{pids[0]}/exe").split()[0]
        if actual != GNSS_SHA256:
            raise MotionError("当前启动中的定位环境尚未准备。重启会清除临时设置，请先重新准备")
        if os.environ.get("SPORTWORLD_GPS_TRANSPORT") == "grpc":
            from emulator_gps import discover
            self.adb("emu", "grpc", str(PORT + 3000))
            discover(AVD, PORT)
        self.verify_boot()
        return {"boot_id": self.boot_id, "gnss_executable_sha256": actual,
                "gps_transport": os.environ.get("SPORTWORLD_GPS_TRANSPORT", "nmea"),
                "login_check": "official home or running activity; exact run mode rechecked before input",
                "gps_check": "runtime GNSS identity only; App GPS start eligibility remains unverified until the App accepts start"}


class StartLocationKeepalive:
    """Prepare an idle fix, then stop observations once the App run page opens."""
    def __init__(self, device, point, altitude_m=None, app_run_mode=None):
        self.device, self.point = device, point
        self.altitude_m = altitude_m
        self.app_run_mode = validate_mode(app_run_mode) if app_run_mode is not None else None
        self.running_seen = False
        self.last_point = point
        self.connection = None
        self.error = None
        self.stop_event = threading.Event()
        self.thread = None
        self.previous_handler = None
        self.fix_index = 0

    def failed(self, signum, frame):
        if not self.stop_event.is_set():
            raise MotionError("等待开始时定位保活失败，已停止") from self.error

    def update(self):
        self.device.verify_boot()
        if (self.app_run_mode is not None and not self.running_seen
                and running_activity_matches(self.device.activity(), self.app_run_mode)):
            self.running_seen = True
            print("App 跑步页面已打开，等待你确认同步；定位输入已暂停。", flush=True)
        if self.running_seen:
            return
        longitude, latitude = self.point
        point = self.point
        if self.fix_index:
            # Sub-metre fix variation, with zero reported speed, before the run.
            point = (longitude+.6*math.sin(self.fix_index*.7)/(111320*math.cos(math.radians(latitude))),
                     latitude+.6*math.cos(self.fix_index*.9)/111320)
        self.fix_index += 1
        if self.altitude_m is None:
            gps(self.connection, point, 0)
        else:
            gps(self.connection, point, 0, altitude_m=self.altitude_m)
        self.last_point = point

    def watch(self):
        while not self.stop_event.wait(1):
            try:
                self.update()
            except Exception as error:
                self.error = error
                if not self.stop_event.is_set():
                    os.kill(os.getpid(), signal.SIGUSR1)
                return

    def __enter__(self):
        self.previous_handler = signal.signal(signal.SIGUSR1, self.failed)
        try:
            self.connection = Console(PORT)
            identify(self.connection)
            self.update()
            self.thread = threading.Thread(target=self.watch)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        self.stop_event.set()
        handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join()
            if self.connection is not None:
                self.connection.close()
        finally:
            signal.signal(signal.SIGUSR1, self.previous_handler)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if exc_type is None and self.error is not None:
            raise MotionError("等待开始时定位保活失败，已停止") from self.error


class BootMonitor:
    def __init__(self, device, *, outdoor_start=False):
        self.device = device
        self.outdoor_start = outdoor_start
        self.input_started = None
        self.app_start_observed_s = None
        self.app_start_observed_at = None
        self.app_start_last_absent_s = 0.0
        self.error = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        while not self.stop_event.wait(.5):
            try:
                self.device.verify_boot()
                if (self.outdoor_start and self.input_started is not None
                        and self.app_start_observed_s is None):
                    activity = self.device.activity()
                    elapsed = time.monotonic()-self.input_started
                    if running_activity_matches(activity, "outdoor"):
                        self.app_start_observed_at = now().isoformat()
                        self.app_start_observed_s = elapsed
                    else:
                        self.app_start_last_absent_s = elapsed
            except Exception as error:
                self.error = error
                return

    def check(self):
        if self.error is not None:
            raise MotionError("运行中的设备身份检查失败") from self.error

    def begin_input(self, started):
        self.input_started = started

    def __enter__(self):
        self.device.verify_boot()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(timeout=3)


@contextmanager
def interruptible():
    def stop(signum, frame):
        raise KeyboardInterrupt
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, stop)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def recover(device, last_point, baseline, last_altitude_m=None):
    """Only restore the exact original boot; never touch a replacement device."""
    device.verify_boot()
    errors = []
    if last_point is not None:
        connection = None
        try:
            connection = Console(PORT)
            identify(connection)
            device.verify_boot()
            if last_altitude_m is None:
                gps(connection, last_point, 0)
            else:
                gps(connection, last_point, 0, altitude_m=last_altitude_m)
        except Exception as error:
            errors.append("GPS 停止失败 "+str(error))
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError as error:
                    errors.append("GPS 连接关闭失败 "+str(error))
    try:
        restore(PORT, AVD, baseline, identity_check=device.verify_boot)
    except Exception as error:
        errors.append("传感器恢复失败 "+str(error))
    if errors:
        raise MotionError("；".join(errors))



def prepare_gps_channel(connection):
    """Open the configured transport without publishing a location observation."""
    if os.environ.get("SPORTWORLD_GPS_TRANSPORT", "nmea") == "grpc":
        from emulator_gps import CachedGps
        if not hasattr(connection, "cached_gps"):
            connection.cached_gps = CachedGps(AVD, PORT)


def gps_anchor_offset(point, sample):
    """Return a finite sub-metre constant observation offset from a plan sample."""
    if (not isinstance(point, (tuple, list)) or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point)):
        raise MotionError("准备定位坐标无效，请重新准备。")
    model_point = (sample["longitude"], sample["latitude"])
    if not all(math.isfinite(value) for value in model_point) or metres(model_point, point) >= 1:
        raise MotionError("准备定位与计划起点相差超过 1 米，请先结束 App 跑步并重新准备。")
    return (point[0]-model_point[0], point[1]-model_point[1])


def transmit(samples, connection, monitor, metrics, clock=time.monotonic, sleep=time.sleep,
             *, outdoor_start=False):
    """Use one monotonic clock; drop overdue frames instead of queueing bursts."""
    started = clock()
    if outdoor_start:
        monitor.begin_input(started)
        metrics.update(warmup_s=8.0, app_start_deadline_s=20.0,
                       app_start_poll_interval_s=.5,
                       requested_pause_elapsed_s=samples[-1]["t_s"]-12,
                       app_window_model_source="Observed App start through scheduled pause cue; model estimate only",
                       app_pause_observed_s=None)
    index, last_gps_second, previous_actual, last_progress_second = 0, -1, None, -1
    window_acceleration_sq, window_gyro_sq, window_cadence, window_count = 0.0, 0.0, 0.0, 0
    last_progress_time = 0.0
    metrics.setdefault("max_wake_lateness_s", 0.0)
    metrics.setdefault("max_gps_batch_s", 0.0)
    while index < len(samples):
        expected = samples[index]["t_s"]
        remaining = started+expected-clock()
        if remaining > 0:
            sleep(remaining)
        actual = clock()-started
        late = actual-expected
        metrics["max_wake_lateness_s"] = max(metrics["max_wake_lateness_s"], late)
        frame = {"scheduled_t_s": expected, "wake_actual_t_s": actual,
                 "previous_completed_t_s": previous_actual, "selected_sample_t_s": None,
                 "gps_batch_s": 0.0, "channel_batch_s": None, "completed_t_s": None}
        metrics["last_frame"] = frame
        if metrics.get("elapsed_before_segment_s", 0) + actual > 300:
            raise MotionError("累计输入已达到 300 秒，已停止")
        if late > .25:
            raise MotionError("输入延迟超过 250 毫秒，已停止")
        target = min(len(samples)-1, max(index, int(actual*HZ)))
        metrics["missed_frames"] += target-index
        index = target
        sample = samples[index]
        frame["selected_sample_t_s"] = sample["t_s"]
        monitor.check()
        operator_action, warmup_remaining = None, 0.0
        if outdoor_start:
            observed = monitor.app_start_observed_s
            warmup_remaining = max(0.0, 8-actual)
            if (observed is None and actual >= 20) or (observed is not None and observed > 20):
                raise MotionError("预跑开始后 20 秒内未确认户外跑步页面，输入已停止，请核验 App")
            if observed is not None and "app_start_observed_s" not in metrics:
                metrics.update(app_start_observed_s=observed,
                               app_start_observed_at=monitor.app_start_observed_at,
                               app_start_last_absent_s=monitor.app_start_last_absent_s,
                               app_start_observation_resolution_s=observed-monitor.app_start_last_absent_s,
                               app_start_observed_early=observed < 8)
                start_index = min(len(samples)-1, max(0, round(observed*HZ)))
                metrics["app_start_model_distance_m"] = samples[start_index]["distance_m"]
                if observed < 8:
                    print("App 提前开始了户外跑步，本次记录包含预跑阶段，请核验原生配速图。", flush=True)
            pause_at = metrics["requested_pause_elapsed_s"]
            if actual >= pause_at:
                operator_action = "pause_app"
                metrics.setdefault("pause_cue_emitted_at", now().isoformat())
            elif observed is None:
                operator_action = "warmup" if actual < 8 else "start_app"
            if operator_action != metrics.get("operator_action"):
                if operator_action == "start_app":
                    print("\a预跑已完成，现在请手动点击 App 的 GO。", flush=True)
                elif operator_action == "pause_app":
                    print("\a请立即在 App 暂停并长按结束、确认，剩余连续输入结束后恢复传感器。", flush=True)
            metrics["operator_action"] = operator_action
            if observed is not None:
                end_index = min(index, max(0, round(pause_at*HZ)))
                metrics["app_window_model_distance_m"] = max(0.0, samples[end_index]["distance_m"]-metrics["app_start_model_distance_m"])
                metrics["app_window_model_duration_s"] = max(0.0, min(actual, pause_at)-observed)
        second = int(sample["t_s"])
        gps_offset = metrics.get("gps_offset_degrees", (0.0, 0.0))
        point = (sample["longitude"]+gps_offset[0], sample["latitude"]+gps_offset[1])
        if second > last_gps_second or index == len(samples)-1:
            bearing = (90-math.degrees(sample.get("heading_rad", math.pi/2))) % 360
            gps_started = clock()
            try:
                if "altitude_m" in sample:
                    gps(connection, point, sample["speed_m_s"]/(1852/3600), bearing_degrees=bearing,
                        altitude_m=sample["altitude_m"])
                else:
                    gps(connection, point, sample["speed_m_s"]/(1852/3600), bearing_degrees=bearing)
            finally:
                gps_completed = clock()
                frame["gps_batch_s"] = gps_completed-gps_started
                frame["completed_t_s"] = gps_completed-started
                metrics["max_gps_batch_s"] = max(metrics["max_gps_batch_s"], frame["gps_batch_s"])
            if "altitude_m" in sample:
                metrics["last_altitude_m"] = sample["altitude_m"]
            metrics["last_point"] = point
            metrics["gps_samples"] += 1
            last_gps_second = second
        from sensor_channels import sample_sensor_inputs
        channel_values = sample_sensor_inputs(sample)
        channel_started = clock()
        try:
            monitor.check()
            set_sensor_values(connection, channel_values)
            monitor.check()
        finally:
            channel_completed = clock()
            frame["channel_batch_s"] = channel_completed-channel_started
            frame["completed_t_s"] = channel_completed-started
            metrics["max_channel_batch_s"] = max(metrics.get("max_channel_batch_s", 0), frame["channel_batch_s"])
        metrics["sensor_channels_sent"] = list(channel_values)
        completed = clock()-started
        frame["completed_t_s"] = completed
        window_acceleration_sq += sum(value*value for value in sample["acceleration"])
        window_gyro_sq += sum(value*value for value in sample["gyroscope"])
        window_cadence += sample.get("cadence_nominal_spm", 0)
        window_count += 1
        metrics["frames"] += 1
        metrics["sent_distance_m"] = sample["distance_m"]
        metrics["input_elapsed_s"] = completed
        metrics["max_lateness_s"] = max(metrics["max_lateness_s"], actual-sample["t_s"])
        if previous_actual is not None:
            metrics["max_frame_gap_s"] = max(metrics["max_frame_gap_s"], completed-previous_actual)
        previous_actual = completed
        if completed-sample["t_s"] > .25 or metrics["max_frame_gap_s"] > .25:
            raise MotionError("实际输入间隔超过 250 毫秒，已停止")
        if completed >= 20 and metrics["missed_frames"]/(metrics["frames"]+metrics["missed_frames"]) > .01:
            raise MotionError("漏发比例超过 1%，已停止")
        if second > last_progress_second or index == len(samples)-1:
            from ui_protocol import publish_progress
            planned = metrics.get("planned", {})
            publish_progress({"phase": "sending", "position_xy": sample.get("position_m"),
                              "distance_m": sample.get("arc_m", sample["distance_m"]),
                              "elapsed_s": metrics.get("elapsed_before_segment_s", 0)+completed,
                              "speed_m_s": sample["speed_m_s"],
                              "cadence_spm": window_cadence/window_count,
                              "step_length_m": sample.get("step_length_m"),
                              "step_count_model": sample.get("step_count_model"),
                              "altitude_m": sample.get("altitude_m"),
                              "ground_altitude_m": sample.get("ground_altitude_m"),
                              "acceleration_rms_m_s2": math.sqrt(window_acceleration_sq/window_count),
                              "gyro_rms_rad_s": math.sqrt(window_gyro_sq/window_count),
                              "interval_imu_hz": window_count/(completed-last_progress_time) if completed-last_progress_time >= .2 else None,
                              "lateness_s": max(0.0, actual-sample["t_s"]),
                              "planned_total_distance_m": planned.get("offset_m", 0)+planned.get("distance_m", samples[-1]["distance_m"]),
                              "imu_hz": (metrics["frames"]-1)/max(completed, 1e-9),
                              "gps_hz": (metrics["gps_samples"]-1)/max(completed, 1e-9),
                              "missed_frames": metrics["missed_frames"],
                              "frames": metrics["frames"], "gps_samples": metrics["gps_samples"],
                              "max_channel_batch_s": metrics["max_channel_batch_s"],
                              "max_wake_lateness_s": metrics["max_wake_lateness_s"],
                              "max_gps_batch_s": metrics["max_gps_batch_s"],
                              "max_frame_gap_s": metrics["max_frame_gap_s"],
                              "max_lateness_s": metrics["max_lateness_s"],
                              "operator_action": operator_action,
                              "warmup_remaining_s": warmup_remaining,
                              "app_start_observed_s": metrics.get("app_start_observed_s"),
                              "app_window_model_distance_m": metrics.get("app_window_model_distance_m")})
            last_progress_second = second
            last_progress_time = completed
            window_acceleration_sq, window_gyro_sq, window_cadence, window_count = 0.0, 0.0, 0.0, 0
        index += 1
    metrics["observed_imu_hz"] = (metrics["frames"]-1)/max(metrics["input_elapsed_s"], 1e-9)
    metrics["observed_gps_hz"] = (metrics["gps_samples"]-1)/max(metrics["input_elapsed_s"], 1e-9)


def numeric_input(prompt, low, high, *, step=None, unit=""):
    try:
        step = step or (1 if "数量" in prompt or "单位米" in prompt else .01)
        value = float(ask(prompt+"\n", control={"type": "number", "min": low, "max": high,
                                                "step": step, "unit": unit}).strip())
    except ValueError as error:
        raise MotionError("请输入数字") from error
    if not math.isfinite(value) or not low <= value <= high:
        raise MotionError(f"数值应在 {low} 到 {high} 之间")
    return value


def parse_time(value):
    parts = value.strip().replace("：", ":").split(":")
    if len(parts) == 1:
        result = float(parts[0])
    elif len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and 0 <= int(parts[1]) < 60:
        result = int(parts[0])*60+int(parts[1])
    else:
        raise MotionError("时间格式应为 分:秒，例如 4:10")
    if not math.isfinite(result) or not 0 < result <= 3600:
        raise MotionError("时间应大于 0 且不超过 60 分钟")
    return result


def record_result(path):
    path = Path(path).resolve()
    if path.parent != RUNS.resolve() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
        raise MotionError("请选择 private/runs 中本工具生成的记录")
    state = json.loads(path.read_text())
    mode = validate_mode(state.get("mode", "campus"))
    if state.get("state") == "cancelled_preparation":
        raise MotionError("这次准备已取消，无需登记跑步结果")
    if state.get("state") == "accepted":
        raise MotionError("这条记录已登记为计分成功")
    device = Device()
    require_app_idle(device)
    print("结果登记仅保存本地核对信息，不会改变 App 成绩。")
    outcomes = ("已保存户外记录", "未产生跑步记录") if mode == "outdoor" else ("已计分", "未计分", "未产生跑步记录")
    choices = [(label, label) for label in outcomes]
    if web_active():
        choices.append(("暂不登记，返回控制台", "defer"))
    outcome = choose("输入 "+"、".join(outcomes)+"\n", choices,
                     text="请先在 App 结束本次跑步，再点选 App 实际显示的结果。").strip()
    if outcome == "defer" and web_active():
        print("已返回控制台，成绩保持未核验。")
        return
    if outcome not in outcomes:
        raise MotionError("结果未确认，记录继续保留为待核验")
    result = {"source": "operator_confirmed_app_outcome" if web_active() else "operator_entered_from_app", "recorded_at": now().isoformat(),
              "credited": mode == "campus" and outcome == "已计分", "label": outcome}
    if outcome != "未产生跑步记录" and not web_active():
        result["distance_km"] = numeric_input("App 显示的距离，单位公里", 0, 5)
        result["duration_s"] = parse_time(ask("App 显示的有效时长，按 分:秒 填写，例如 4:23\n",
                                             text="点选 App 实际显示的有效时长。", control={"type": "time"}))
        result["pace_s_km"] = parse_time(ask("App 显示的平均配速，按 分:秒 填写，例如 4:10\n",
                                            text="点选 App 实际显示的平均配速。", control={"type": "time"}))
        if mode == "campus":
            result["checkpoints_completed"] = numeric_input("App 显示已完成的点位数量", 0, 20)
            result["checkpoints_total"] = numeric_input("App 显示点位总数", 1, 20)
            if result["checkpoints_completed"] > result["checkpoints_total"] or any(not result[k].is_integer() for k in ("checkpoints_completed", "checkpoints_total")):
                raise MotionError("点位数量应为整数，完成数量应小于或等于总数")
        result["pace_target_delta_s_km"] = result["pace_s_km"]-state["pace_target_s_km"]
    require_app_idle(device)
    state["app_session_unresolved"] = False
    state["mode"] = mode
    state["result"] = result
    state["state"] = "accepted" if result["credited"] else "reviewed"
    atomic_json(path, state)
    print("结果已登记，数据来源为 App 页面人工核对。")


def interactive_run(route_path, data, reference, args):
    mode = validate_mode(getattr(args, "mode", "outdoor" if getattr(args, "outdoor_test", False) else "campus"))
    seed = getattr(args, "seed", 0)
    with RunLock(), interruptible():
        check_daily_credit(mode=mode)
        route_review = validate_route_review(route_path, data) if mode == "campus" else None
        device = Device()
        readiness = device.preflight()
        require_app_idle(device, home=True)
        connection = Console(PORT)
        try:
            identify(connection)
            from sensor_channels import COUPLED_SENSORS
            baseline = {sensor: values(connection, sensor) for sensor in COUPLED_SENSORS}
        finally:
            connection.close()
        if any(abs(value) > .1 for value in baseline["gyroscope"]):
            raise MotionError("当前陀螺仪尚未静止，请先结束其他输入程序")
        samples, metadata = build_samples(data, reference, args.distance, args.pace, args.cadence,
                                          gravity=baseline["acceleration"], seed=seed,
                                          magnetic_field=baseline["magnetic-field"])
        verify_samples(samples, metadata)
        run_id = uuid.uuid4().hex
        state_path = RUNS / (run_id+".json")
        state = {"schema": 1, "id": run_id, "date": now().date().isoformat(),
                 "created_at": now().isoformat(), "mode": mode, "state": "armed", "readiness": readiness,
                 "route_review": route_review, "pace_target_s_km": args.pace,
                 "pace_profile_seed": seed, "route_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
                 "reference_sha256": reference.sha256, "segments": [], "result": None,
                 "phase": "preparing", "first_sync_confirmed_at": None}
        atomic_json(state_path, state)
        try:
            total_distance, total_duration, phase = 0.0, 0.0, 0.0
            while True:
                from motion_report import expected_timeline
                from ui_protocol import publish_plan
                publish_plan({"segment_index": len(state["segments"]),
                              "timeline": expected_timeline(samples, sum(segment.get("input_elapsed_s", 0) for segment in state["segments"]))})
                print(f"计划输入 {metadata['distance_m']:.0f} 米，用时 {metadata['duration_s']:.1f} 秒。")
                model_start = (samples[0]["longitude"], samples[0]["latitude"])
                previous_metrics = state["segments"][-1] if state["segments"] else None
                outdoor_start = mode == "outdoor" and previous_metrics is None
                if outdoor_start and metadata["duration_s"] <= 32:
                    raise MotionError("首次户外输入需超过 32 秒，请增加距离，留出预跑和结束确认时间。")
                gps_offset = tuple(previous_metrics.get("gps_offset_degrees", (0.0, 0.0))) if previous_metrics else (0.0, 0.0)
                preparation_point = (model_start[0]+gps_offset[0], model_start[1]+gps_offset[1])
                with StartLocationKeepalive(device, preparation_point,
                                            altitude_m=samples[0].get("altitude_m"), app_run_mode=mode) as keepalive:
                    if outdoor_start:
                        require_app_idle(device, home=True)
                        acknowledge("保持 App 在户外跑首页，按回车开始预跑\n", "开始预跑，等待 GO 提示",
                                    text="保持 App 在户外跑首页。先开始 8 秒预跑，收到提示后再手动点击 App 的 GO。结束前 12 秒会提示你立即暂停 App，并长按结束、确认。剩余连续输入完成后恢复传感器。",
                                    requires_app_home=True)
                    elif mode == "outdoor":
                        acknowledge("在 App 点击 GO，跑步页面出现后立即按回车\n", "App 已点 GO，开始同步",
                                    text="先等待右侧 App 的 GPS 为绿色，再点击 GO。跑步页面出现后立即点击下方按钮开始同步。若 App 提示“GPS信号弱，无法开始”，请勿启动同步。",
                                    app_run_mode=mode)
                    else:
                        acknowledge("在 App 开始或继续跑步，倒计时结束时立即按回车\n", "倒计时已结束，开始同步",
                                    text="先等待右侧 App 的 GPS 为绿色，再在 App 开始跑步，倒计时结束时点击下方按钮。若 App 提示“GPS信号弱，无法开始”，请勿启动同步。",
                                    app_run_mode=mode)
                    if state["first_sync_confirmed_at"] is None:
                        state["first_sync_confirmed_at"] = now().isoformat()
                    state["phase"] = "sync_confirmed"
                    atomic_json(state_path, state)
                    acknowledged = time.monotonic()
                    ack_wall = now().isoformat()
                initial_point = keepalive.last_point
                if not isinstance(initial_point, (tuple, list)):
                    initial_point = preparation_point
                if previous_metrics is None:
                    gps_offset = gps_anchor_offset(initial_point, samples[0])
                    adjustment_m = metres(model_start, initial_point)
                else:
                    adjustment_m = previous_metrics.get("gps_anchor_adjustment_m", metres(model_start, preparation_point))
                device.verify_boot()
                if outdoor_start:
                    require_app_idle(device, home=True)
                elif not running_activity_matches(device.activity(), mode):
                    raise MotionError("App 当前未处于跑步页面，请核验这次记录")
                state["state"] = "running"
                state["phase"] = "input"
                metrics = {"gps_samples": 0, "frames": 0, "missed_frames": 0,
                           "max_lateness_s": 0.0, "max_frame_gap_s": 0.0,
                           "acknowledged_at": ack_wall, "last_point": initial_point,
                           "planned": metadata, "input_elapsed_s": 0.0,
                           "gps_offset_degrees": gps_offset, "gps_anchor_adjustment_m": adjustment_m}
                if "altitude_m" in samples[0]:
                    metrics["last_altitude_m"] = samples[0]["altitude_m"]
                metrics["elapsed_before_segment_s"] = sum(segment.get("input_elapsed_s", 0) for segment in state["segments"])
                state["segments"].append(metrics)
                atomic_json(state_path, state)
                connection = None
                transmission_error = None
                try:
                    connection = Console(PORT)
                    identify(connection)
                    # Open the transport without publishing an extra pre-clock fix.
                    prepare_gps_channel(connection)
                    monitor_context = BootMonitor(device, outdoor_start=True) if outdoor_start else BootMonitor(device)
                    with monitor_context as monitor:
                        metrics["ack_to_input_delay_s"] = time.monotonic()-acknowledged
                        if outdoor_start:
                            transmit(samples, connection, monitor, metrics, outdoor_start=True)
                        else:
                            transmit(samples, connection, monitor, metrics)
                except BaseException as error:
                    transmission_error = error
                    raise
                finally:
                    old_handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
                    try:
                        if connection is not None:
                            try:
                                connection.close()
                            except Exception as error:
                                metrics["connection_close_error"] = str(error)
                        try:
                            if "last_altitude_m" in metrics:
                                recover(device, metrics["last_point"], baseline, last_altitude_m=metrics["last_altitude_m"])
                            else:
                                recover(device, metrics["last_point"], baseline)
                            metrics["restoration_verified"] = True
                            from ui_protocol import publish_progress
                            publish_progress({"phase": "restored", "restoration_verified": True})
                        except Exception as error:
                            metrics["restoration_verified"] = False
                            metrics["restoration_error"] = str(error)
                            if transmission_error is None:
                                raise
                        finally:
                            atomic_json(state_path, state)
                    finally:
                        for sig, handler in old_handlers.items():
                            signal.signal(sig, handler)
                ended = time.monotonic()
                state["phase"] = "awaiting_app_pause"
                atomic_json(state_path, state)
                app_idle = False
                if outdoor_start:
                    try:
                        activity = require_app_idle(device)
                        component = re.escape(PACKAGE) + r"/(?:\.|" + re.escape(PACKAGE) + r"\.)"
                        if re.search(component + r"(?:mvi\.home\.HomeActivity|mvi\.run\.RunHistoryDetailActivity)(?=[\s}]|$)", activity) is None:
                            raise MotionError("尚未确认 App 已返回首页或跑步结果页，请核对当前页面。")
                    except MotionError as error:
                        metrics["app_idle_check_after_input_error"] = str(error)
                    else:
                        app_idle = True
                        metrics["app_idle_observed_after_input_at"] = now().isoformat()
                if not app_idle:
                    if outdoor_start:
                        print("\a输入已停止，传感器已恢复。请核对 App 已暂停或结束。", flush=True)
                        acknowledge("App 暂停或结束后按回车\n", "App 已暂停或结束，继续",
                                    text="本段输入已结束，传感器已恢复。请核对右侧 App 已暂停或结束，再继续。是否保存成功仍需在 App 核验。")
                        metrics["app_pause_operator_confirmed_at"] = now().isoformat()
                    else:
                        print("\a输入已停止，传感器已恢复。现在请在 App 暂停。", flush=True)
                        acknowledge("暂停后按回车\n", "App 已暂停，继续", text="本段输入已结束，请先在右侧 App 暂停跑步。")
                    metrics["end_to_pause_ack_delay_s"] = time.monotonic()-ended
                total_distance += metadata["distance_m"]
                total_duration += metadata["duration_s"]
                phase = samples[-1]["phase_s"]
                state.update(state="paused", phase="awaiting_app_distance", input_distance_m=total_distance, input_duration_s=total_duration)
                atomic_json(state_path, state)
                if mode == "outdoor" and web_active():
                    break
                app_distance = numeric_input("App 当前显示距离，单位公里", 0, 5)
                metrics["app_distance_km_at_pause"] = app_distance
                atomic_json(state_path, state)
                if mode == "outdoor" or app_distance >= 1:
                    break
                extra = numeric_input("需要继续补足的输入距离，单位米，例如 50", 1, 1200)
                if total_distance+extra > 1200 or total_duration+extra*args.pace/1000 > 300:
                    raise MotionError("补足会超过单次 1.2 公里或 300 秒限制")
                next_elapsed_offset = samples[-1].get("absolute_time_s", total_duration)
                samples, metadata = build_samples(data, reference, extra, args.pace, args.cadence,
                                                  offset=total_distance, phase=phase, gravity=baseline["acceleration"],
                                                  allow_slower=True, seed=seed, elapsed_offset_s=next_elapsed_offset,
                                                  model_version=metadata.get("motion_model_version", 1),
                                                  elevation=metadata.get("elevation_profile"),
                                                  magnetic_field=metadata["magnetic_field_baseline"],
                                                  magnetic_field_world=metadata["magnetic_field_world"])
                verify_samples(samples, metadata)
                if metadata["duration_s"]+total_duration > 300:
                    raise MotionError("补足后输入时间超过 300 秒")
            state["state"] = "review_pending"
            atomic_json(state_path, state)
            if web_active():
                print("输入流程已完成，请在 App 结束本次跑步。结果可在运行记录中补记，无需填写即可返回。")
            else:
                record_result(state_path)
        except (Exception, KeyboardInterrupt) as error:
            if not state["segments"] and not state["first_sync_confirmed_at"]:
                state["state"] = "preparation_stopped"
            elif (state.get("phase") in ("awaiting_app_pause", "awaiting_app_distance")
                  and state["segments"]
                  and all(segment.get("restoration_verified") is True for segment in state["segments"])):
                state["state"] = "review_pending"
            else:
                state["state"] = "failed"
                state["phase"] = "awaiting_app_pause_after_failure"
                state["app_session_unresolved"] = True
            state["error"] = str(error) or "手动中止"
            atomic_json(state_path, state)
            if state["state"] == "preparation_stopped":
                raise MotionError(f"准备已停止，App 是否开始尚未核实。请在控制台核对或取消准备。记录保存在 {state_path}") from error
            raise MotionError(f"运行已停止，请核验 App 和恢复结果。记录保存在 {state_path}") from error
    return 0
