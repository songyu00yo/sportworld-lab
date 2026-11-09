"""Loopback-only operator panel for this project's isolated Android tablet."""
import argparse
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import uuid
import webbrowser

from emulator_motion import MotionError
from elevation_profile import USER_REFERENCE_PATH
from motion_reference import ROOT, REFERENCE_DIR
from run_session import Device, PACKAGE, RUNS, RunLock, preparation_cancellable, atomic_json, check_daily_credit, now, parse_time, optional_outdoor_result, require_app_idle, running_activity_matches, validate_route_review

ASSETS = ROOT / "control_panel"
ROUTE = ROOT / ".lab/routes/neusoft-track-route.json"
SERVER_STATE = ROOT / ".lab/control-panel.json"
AUTH_STATE = ROOT / ".lab/control-panel-auth.json"
GENERATED_STATE = ROOT / ".lab/control-panel-generation.json"
DEFAULT_PORT = 18762


def settings_from(payload):
    if not isinstance(payload, dict):
        raise MotionError("参数格式无效")
    result = {}
    for key, default, low, high in (("pace", 250, 200, 600), ("cadence", 175, 120, 210), ("distance", 1050, 1000, 1200)):
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise MotionError(f"{key} 参数超出范围")
        result[key] = float(value)
    if payload.get("auto_cadence", False) is True:
        result["cadence"] = float(max(140, min(190, round(225-.2*result["pace"]))))
    if result["distance"]*result["pace"]/1000 > 300:
        raise MotionError("当前距离和配速需要超过 5 分钟，请减少距离或提高配速")
    return result


def fingerprint(settings, seed=0):
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise MotionError("节奏编号无效，请重新生成计划")
    return {"settings": settings, "seed": seed, "route_sha256": hashlib.sha256(ROUTE.read_bytes()).hexdigest(),
            "profile_sha256": hashlib.sha256((REFERENCE_DIR/"template.json").read_bytes()).hexdigest(),
            "elevation_reference_sha256": hashlib.sha256(USER_REFERENCE_PATH.read_bytes()).hexdigest(),
            "implementation_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                      for name in ("motion_reference.py", "run_trajectory.py", "elevation_profile.py", "sensor_channels.py", "stadium_track.py", "run_session.py", "emulator_route.py", "emulator_motion.py", "emulator_gps.py", "motion_report.py")}}


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None
        self.process = None
        self.generated = None
        try:
            generated = json.loads(GENERATED_STATE.read_text())
            if generated.get("panel_schema") == 3 and generated["fingerprint"] == fingerprint(generated["fingerprint"]["settings"], generated["fingerprint"]["seed"]):
                self.generated = generated
        except (MotionError, OSError, ValueError, KeyError, TypeError):
            pass
        self.frames = deque(maxlen=10)
        self.device = Device()
        self.frame_lock = threading.Lock()
        self.device_status = "unknown"
        self.closing = False
        self.shutdown_error = None

    def status(self):
        with self.lock:
            blocked_by_mode = {}
            for mode in ("campus", "outdoor"):
                try:
                    check_daily_credit(mode=mode)
                    blocked_by_mode[mode] = None
                except (MotionError, OSError, ValueError) as error:
                    blocked_by_mode[mode] = str(error)
            pending, recent_results = [], []
            for path in sorted(RUNS.glob("*.json")):
                try:
                    record = json.loads(path.read_text())
                    if record.get("state") not in ("reviewed", "accepted", "cancelled_preparation"):
                        pending.append({"id": path.stem, "date": record.get("date", ""), "state": record.get("state", ""),
                                        "optional": optional_outdoor_result(record),
                                        "app_pause_required": record.get("phase") in ("awaiting_app_pause", "awaiting_app_pause_after_failure")
                                        or (record.get("state") == "failed" and bool(record.get("segments") or record.get("first_sync_confirmed_at"))),
                                        "preparation_cancellable": preparation_cancellable(record),
                                        "resumable": record.get("mode") == "outdoor" and record.get("state") == "failed"
                                        and bool(record.get("segments")) and all(s.get("restoration_verified") is True for s in record.get("segments", []))})
                    elif record.get("result"):
                        result = record["result"]
                        recent_results.append({"id": path.stem, "date": record.get("date", ""),
                                               "recorded_at": result.get("recorded_at", ""),
                                               "label": result.get("label", "已核对"), "distance_km": result.get("distance_km"),
                                               "duration_s": result.get("duration_s"), "pace_s_km": result.get("pace_s_km")})
                except (OSError, ValueError):
                    blocked_by_mode = dict.fromkeys(("campus", "outdoor"), "有一份本地运行记录需要检查")
            return {"job": self.job, "generated": self.generated, "run_blocked": blocked_by_mode["campus"],
                    "closing": self.closing, "shutdown_error": self.shutdown_error,
                    "outdoor_blocked": blocked_by_mode["outdoor"], "device_status": self.device_status,
                    "pending": pending, "app_pause_required": any(row["app_pause_required"] for row in pending),
                    "recent_results": sorted(recent_results, key=lambda row: row["recorded_at"], reverse=True)[:8],
                    "reference_ready": (REFERENCE_DIR/"template.json").exists(),
                    "map_available": (ROOT/".lab/maps/neusoft-stadium-map.png").is_file()}

    def start(self, action, payload):
        with self.lock:
            if self.closing:
                raise MotionError("正在彻底退出，请等待完成。")
            if self.job and self.job["state"] in ("running", "waiting", "stopping"):
                raise MotionError("请先完成或停止当前操作")
            command = [sys.executable, "-B", "-u", str(ROOT/"emulator_route.py"), "--ui-protocol"]
            spec, output_path = None, None
            mode = payload.get("mode", "campus")
            if mode not in ("campus", "outdoor"):
                raise MotionError("请选择校园跑或户外测试模式")
            if action in ("generate", "run"):
                settings = settings_from(payload)
                seed = secrets.randbits(32) if action == "generate" else (self.generated or {}).get("fingerprint", {}).get("seed")
                spec = fingerprint(settings, seed)
                command += ["--route", str(ROUTE), "--pace", str(settings["pace"]),
                            "--cadence", str(settings["cadence"]), "--distance", str(settings["distance"]),
                            "--seed", str(seed)]
                if action == "generate":
                    output_path = ROOT/".lab/validation/panel"/(uuid.uuid4().hex+".jsonl")
                    command += ["--export", str(output_path)]
                else:
                    check_daily_credit(mode=mode)
                    if self.generated is None or self.generated["fingerprint"] != spec:
                        raise MotionError("参数、路线或参考数据已变化，请先重新生成并检查")
                    if self.generated.get("used_for_run"):
                        raise MotionError("本次计划已用于准备，请重新生成下一次节奏")
                    require_app_idle(self.device, home=True)
                    execution = "--outdoor-test" if mode == "outdoor" else "--review-and-run"
                    if mode == "campus":
                        try:
                            validate_route_review(ROUTE,json.loads(ROUTE.read_text()))
                            execution = "--run"
                        except (MotionError,OSError,ValueError):
                            pass
                    command += [execution]
            elif action == "prepare_reference":
                command += ["--prepare-reference"]
            elif action in ("record_result", "resume"):
                run_id = payload.get("run_id", "")
                if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
                    raise MotionError("运行记录编号无效")
                path = RUNS/(run_id+".json")
                if not path.exists():
                    raise MotionError("运行记录不存在")
                if action == "resume":
                    mode = "outdoor"
                    command = [sys.executable, "-B", "-u", str(ROOT/"resume_outdoor.py"), "--run-id", run_id, "--ui-protocol"]
                else:
                    require_app_idle(self.device)
                    command += ["--record-result", str(path)]
            else:
                raise MotionError("不支持的操作")
            if action == "run":
                reserved = {**self.generated, "used_for_run": True}
                atomic_json(GENERATED_STATE, reserved)
                self.generated = reserved
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
                                       env={**os.environ, "SPORTWORLD_GPS_TRANSPORT": "grpc"})
            job = {"id": uuid.uuid4().hex, "action": "run" if action == "resume" else action, "mode": mode, "state": "running", "events": [], "prompt": None,
                   "exit_code": None, "started_at": time.time(), "telemetry": None, "telemetry_history": [], "expected_timeline": []}
            self.process, self.job = process, job
            threading.Thread(target=self.read_process, args=(process, job, spec, output_path), daemon=True).start()
            return job["id"]

    def read_process(self, process, job, spec, output_path):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    message = {"kind": "output", "text": line.rstrip()}
                with self.lock:
                    if message.get("kind") == "plan":
                        plan = message.get("data", {})
                        if plan.get("segment_index") == 0:
                            job["expected_timeline"] = []
                        job["expected_timeline"].extend(plan.get("timeline", []))
                    elif message.get("kind") == "telemetry":
                        current = job.get("telemetry") or {}
                        current.update(message.get("data", {}))
                        current["received_at"] = time.time()
                        job["telemetry"] = current
                        if current.get("phase") == "sending":
                            job["telemetry_history"].append(dict(current))
                            job["telemetry_history"] = job["telemetry_history"][-400:]
                    elif message.get("kind") == "prompt":
                        job["prompt"] = {"id": uuid.uuid4().hex, "text": str(message.get("text", "")),
                                         "control": message.get("control")}
                        job["state"] = "waiting"
                    else:
                        job["events"].append(now().strftime("%H:%M:%S")+"  "+str(message.get("text", "")))
                        job["events"] = job["events"][-24:]
            code = process.wait()
            result = None
            if code == 0 and output_path is not None:
                report = json.loads(output_path.with_suffix(".jsonl.report.json").read_text())
                records = [json.loads(line) for line in output_path.read_text().splitlines()]
                from motion_report import expected_timeline
                stride = max(1, len(records)//180)
                preview = records[::stride]
                if preview[-1] != records[-1]:
                    preview.append(records[-1])
                result = {"panel_schema": 3, "fingerprint": spec, "report_path": str(output_path.with_suffix(".jsonl.report.json")),
                          "expected_timeline": expected_timeline(records),
                          "metadata": report["trajectory"], "checks": report["consistency"],
                          "comparisons": report["comparisons"],
                          "preview": [{"x": s["position_m"][0], "y": s["position_m"][1],
                                       "t": s["t_s"], "speed": s["speed_m_s"], "distance": s["distance_m"],
                                       "cadence": s.get("cadence_nominal_spm")} for s in preview]}
            with self.lock:
                job.update(state="finished" if code == 0 else "stopped" if job["state"] == "stopping" else "failed",
                           exit_code=code, prompt=None)
                if result is not None:
                    atomic_json(GENERATED_STATE, result)
                    self.generated = result
        except Exception as error:
            with self.lock:
                job.update(state="failed", prompt=None)
                job["events"].append("读取操作结果失败，请检查本地报告。"+str(error))
        finally:
            process.stdout.close()
            process.stdin.close()

    def answer(self, payload):
        with self.lock:
            if self.closing:
                raise MotionError("正在彻底退出，请等待完成。")
            if not self.job or self.job["state"] != "waiting" or payload.get("job_id") != self.job["id"]:
                raise MotionError("当前没有待回答的问题")
            prompt = self.job["prompt"]
            if payload.get("prompt_id") != prompt["id"]:
                raise MotionError("这个提示已经处理，请查看新的提示")
            answer = payload.get("answer", "")
            if not isinstance(answer, str) or len(answer) > 300 or "\n" in answer or "\r" in answer:
                raise MotionError("请输入单行内容")
            control = prompt.get("control") or {}
            if control.get("type") == "choice":
                if answer not in [option["value"] for option in control["choices"]]:
                    raise MotionError("请选择当前步骤提供的按钮")
            elif control.get("type") == "number":
                if answer != "" or not control.get("optional"):
                    try:
                        value = float(answer)
                    except ValueError as error:
                        raise MotionError("请点选当前步骤的数值") from error
                    if (not math.isfinite(value) or not control["min"] <= value <= control["max"]
                            or abs(value/control["step"]-round(value/control["step"])) > 1e-7):
                        raise MotionError("数值不在当前步骤允许的范围内")
            elif control.get("type") == "time":
                try:
                    parse_time(answer)
                except (MotionError, ValueError) as error:
                    raise MotionError("请点选有效的分钟和秒数") from error
            app_mode = control.get("requires_app_run_mode")
            if control.get("requires_app_home"):
                require_app_idle(self.device, home=True)
            if app_mode is not None and not running_activity_matches(self.device.activity(), app_mode):
                raise MotionError("App 尚未进入对应的跑步页面。若提示“GPS信号弱，无法开始”，需先让 App 定位达标；当前不会发送运动数据。")
            if self.job.get("action") == "record_result":
                require_app_idle(self.device)
            self.process.stdin.write(answer+"\n")
            self.process.stdin.flush()
            self.job.update(prompt=None, state="running")

    def cancel_preparation(self, payload):
        with self.lock, RunLock():
            if self.closing or (self.job and self.job["state"] in ("running", "waiting", "stopping")):
                raise MotionError("请先停止当前流程，再取消准备")
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
                raise MotionError("运行记录编号无效")
            path = RUNS / (run_id + ".json")
            record = json.loads(path.read_text())
            if not preparation_cancellable(record):
                raise MotionError("这条记录已有同步确认或输入，需核对 App 实际结果")
            activity = self.device.activity()
            if not activity.strip():
                raise MotionError("无法核实 App 当前页面，请确认 App 已打开后重试")
            if any(running_activity_matches(activity, mode) for mode in ("campus", "outdoor")):
                raise MotionError("App 正在跑步，请先核对 App 实际结果")
            if PACKAGE not in activity or "HomeActivity" not in activity:
                raise MotionError("请先回到运动世界首页，再确认取消准备")
            source = payload.get("confirmation_source", "operator_confirmed_app_home")
            if source not in ("operator_confirmed_app_home", "user_reported_no_sync_app_home_verified"):
                raise MotionError("取消准备的确认来源无效")
            record.update(state="cancelled_preparation", phase="cancelled_preparation",
                          cancelled_at=now().isoformat(),
                          cancellation_source=source, app_result_status="unverified", result=None)
            atomic_json(path, record)

    def stop(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.job["state"] = "stopping"
                self.job["prompt"] = None
                os.killpg(self.process.pid, signal.SIGINT)

    def drain(self):
        with self.lock:
            self.closing = True
            self.shutdown_error = None
            self.stop()
        # Finish any in-flight screenshot/gesture before the guest is closed.
        with self.frame_lock:
            pass

    def request_shutdown(self):
        with self.lock:
            if self.closing:
                return
            self.drain()
            try:
                with (ROOT / ".lab/runtime-shutdown.log").open("ab") as log:
                    subprocess.Popen([sys.executable, "-B", str(ROOT / "runtime_manager.py"), "stop"],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                self.closing = False
                raise MotionError("退出程序未能启动，请使用“彻底退出校园跑.command”。") from None

    def cancel_shutdown(self):
        with self.lock:
            self.closing = False
            self.shutdown_error = "退出未完成，控制台已保留。请重新点击彻底退出，或使用文件夹中的退出入口。"

    def open_app(self):
        with self.frame_lock:
            if self.closing:
                raise MotionError("正在彻底退出，请等待完成。")
            if self.job and self.job["action"] == "run" and self.job["state"] in ("running", "waiting", "stopping"):
                raise MotionError("输入流程进行中，请先完成或停止当前流程")
            if PACKAGE not in self.device.activity():
                self.device.adb("shell", "am", "start", "-n", PACKAGE+"/.mvi.splash.SplashActivity", timeout=8)

    def open_viewer(self):
        with self.frame_lock:
            if self.closing:
                raise MotionError("正在彻底退出，请等待完成。")
            # A viewer request must never enqueue a full environment restart.
            from runtime_manager import Runtime
            Runtime().open_viewer()
            return "已打开测试平板窗口。若已打开，可按 ⌘ + Tab 切换到 scrcpy。"

    def snapshot(self):
        with self.frame_lock:
            if self.closing:
                raise MotionError("正在彻底退出，已停止读取画面。")
            before = self.device.activity()
            if "com.tencent.mm/.ui.AccountExpiredUI" in before:
                self.device_status = "wechat_expired"
                raise MotionError("微信登录已过期。请在测试平板窗口重新登录，再返回运动世界。")
            if PACKAGE not in before:
                self.device_status = "other_app"
                raise MotionError("请在测试平板中打开运动世界，即可显示画面")
            self.device_status = "sportworld_foreground"
            try:
                result = subprocess.run(self.device.prefix+["exec-out", "screencap", "-p"], capture_output=True, timeout=4)
            except subprocess.TimeoutExpired as error:
                raise MotionError("画面读取超时，稍后会再次尝试") from error
            after = self.device.activity()
            if before != after or PACKAGE not in after:
                raise MotionError("页面正在切换，稍后会刷新画面")
            png = result.stdout
            if result.returncode or not 24 <= len(png) <= 5*1024*1024 or png[:8] != b"\x89PNG\r\n\x1a\n":
                raise MotionError("暂时无法读取模拟器画面")
            width, height = struct.unpack(">II", png[16:24])
            if not 1 <= width <= 4096 or not 1 <= height <= 4096:
                raise MotionError("模拟器画面尺寸异常")
            frame = {"id": uuid.uuid4().hex, "width": width, "height": height, "activity": after, "at": time.monotonic()}
            self.frames.append(frame)
            return png, frame["id"]

    def interact(self, payload):
        with self.frame_lock:
            if self.closing:
                raise MotionError("正在彻底退出，请等待完成。")
            frame = next((f for f in self.frames if f["id"] == payload.get("frame_id")), None)
            if frame is None or time.monotonic()-frame["at"] > 15:
                raise MotionError("画面已过期，请等待刷新后再操作")
            if self.device.activity() != frame["activity"]:
                raise MotionError("页面已切换，请等待刷新后再操作")
            kind = payload.get("kind")
            if kind == "back":
                self.device.adb("shell", "input", "keyevent", "4")
                return
            if kind not in ("tap", "swipe"):
                raise MotionError("不支持的手势")
            coordinates = []
            keys = ("x", "y", "end_x", "end_y") if kind == "swipe" else ("x", "y")
            for i, key in enumerate(keys):
                value = payload.get(key)
                bound = frame["width"] if i % 2 == 0 else frame["height"]
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value < bound:
                    raise MotionError("点击位置超出画面")
                coordinates.append(str(min(bound-1, round(value))))
            if kind == "swipe":
                duration = payload.get("duration_ms", 300)
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 100 <= duration <= 3000:
                    raise MotionError("手势时长超出范围")
                coordinates.append(str(round(duration)))
            self.device.adb("shell", "input", kind, *coordinates, timeout=5)


def make_handler(controller, token, instance_id):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never put the bootstrap URL/cookie in HTTP access logs.

        def authorized(self):
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [""])[0]
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                if not supplied and "campus_control" in cookie:
                    supplied = cookie["campus_control"].value
            except Exception:
                return False
            return hmac.compare_digest(supplied, token)

        def respond(self, code, body, content_type="application/json; charset=utf-8", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.authorized():
                self.respond(403, {"error": "请通过本机启动入口打开控制台"})
                return
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self.respond(200, {"service": "campus-control", "instance": instance_id})
                elif path == "/api/status":
                    self.respond(200, controller.status())
                elif path == "/api/frame":
                    png, frame_id = controller.snapshot()
                    self.respond(200, png, "image/png", {"X-Frame-Id": frame_id})
                elif path == "/track-map.png":
                    self.respond(200, (ROOT/".lab/maps/neusoft-stadium-map.png").read_bytes(), "image/png")
                elif path in ("/", "/panel.css", "/panel.js", "/progress.js", "/charts.js", "/workflow.js"):
                    name = "index.html" if path == "/" else path[1:]
                    mime = "text/html" if path == "/" else "text/css" if path.endswith(".css") else "text/javascript"
                    headers = {"Set-Cookie": f"campus_control={token}; HttpOnly; SameSite=Strict; Path=/"} if path == "/" else {}
                    self.respond(200, (ASSETS/name).read_bytes(), mime+"; charset=utf-8", headers)
                else:
                    self.respond(404, {"error": "页面不存在"})
            except (MotionError, OSError, ValueError) as error:
                self.respond(409, {"error": str(error)})

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.authorized() or self.headers.get("Origin") != expected_origin:
                self.respond(403, {"error": "请求来源无效"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise MotionError("请求格式无效")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise MotionError("请求格式无效")
                path = urlparse(self.path).path
                if path == "/api/action":
                    result = {"job_id": controller.start(payload.get("action"), payload)}
                elif path == "/api/answer":
                    controller.answer(payload)
                    result = {"ok": True}
                elif path == "/api/cancel-preparation":
                    controller.cancel_preparation(payload)
                    result = {"ok": True}
                elif path == "/api/stop":
                    controller.stop()
                    result = {"ok": True}
                elif path == "/api/shutdown":
                    controller.request_shutdown()
                    result = {"ok": True}
                elif path == "/api/drain":
                    controller.drain()
                    result = {"ok": True}
                elif path == "/api/cancel-shutdown":
                    controller.cancel_shutdown()
                    result = {"ok": True}
                elif path == "/api/close-server":
                    with controller.lock:
                        if not controller.closing or (controller.process is not None and controller.process.poll() is None):
                            raise MotionError("请先停止当前流程，再退出控制台。")
                    self.respond(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                elif path == "/api/input":
                    controller.interact(payload)
                    result = {"ok": True}
                elif path == "/api/open-app":
                    controller.open_app()
                    result = {"ok": True}
                elif path == "/api/open-viewer":
                    result = {"message": controller.open_viewer()}
                else:
                    self.respond(404, {"error": "操作不存在"})
                    return
                self.respond(200, result)
            except (MotionError, OSError, ValueError, KeyError) as error:
                self.respond(409, {"error": str(error)})
    return Handler


def serve(port, *, token=None):
    if token is None:
        try:
            candidate = json.loads(AUTH_STATE.read_text()).get("token")
            token = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", candidate) else None
        except (OSError, ValueError, AttributeError):
            token = None
    token = secrets.token_urlsafe(32) if token is None else token
    atomic_json(AUTH_STATE, {"token": token})
    instance = uuid.uuid4().hex
    controller = Controller()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(controller, token, instance))
    server.daemon_threads = True
    atomic_json(SERVER_STATE, {"pid": os.getpid(), "port": server.server_port, "token": token, "instance": instance})
    def shutdown(signum, frame):
        controller.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, shutdown)
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        controller.stop()
        if controller.process is not None:
            try:
                controller.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                pass  # Pending run remains visible for explicit recovery.
        server.server_close()
        if SERVER_STATE.exists() and json.loads(SERVER_STATE.read_text()).get("instance") == instance:
            SERVER_STATE.unlink()


def running_url():
    try:
        state = json.loads(SERVER_STATE.read_text())
        port = int(state["port"])
        if not 1024 <= port <= 65535:
            return None
        base = f"http://127.0.0.1:{port}"
        request = Request(base+"/health", headers={"Cookie": "campus_control="+state["token"]})
        with urlopen(request, timeout=1) as response:
            health = json.load(response)
        if health.get("service") == "campus-control" and health.get("instance") == state["instance"]:
            return base+"/?token="+state["token"]
    except (OSError, ValueError, KeyError):
        return None
    return None


def launch(port=DEFAULT_PORT, open_browser=True):
    url = running_url()
    if url is None:
        log_path = ROOT/".lab/control-panel.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_path.open("ab") as log:
            child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--serve", "--port", str(port)],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(30):
            time.sleep(.1)
            url = running_url()
            if url:
                break
            if child.poll() is not None:
                raise MotionError("控制台启动失败，请检查本地端口或 .lab/control-panel.log")
    if not url:
        raise MotionError("控制台启动超时")
    if open_browser:
        webbrowser.open(url)
    return url


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        if args.serve:
            serve(args.port)
        else:
            launch(args.port)
            print("校园跑控制台已打开。")
    except (MotionError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
