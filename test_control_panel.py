"""Local HTTP/controller tests with explicit device doubles; no app input."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import control_panel as panel
from emulator_motion import MotionError


class ParameterTests(unittest.TestCase):
    def test_default_and_auto_cadence(self):
        self.assertEqual(panel.settings_from({}), {"pace": 250, "cadence": 175, "distance": 1050})
        self.assertEqual(panel.settings_from({"pace": 260, "auto_cadence": True})["cadence"], 173)

    def test_bad_types_and_time_budget_rejected(self):
        for payload in ({"pace": True}, {"cadence": float("nan")}, {"distance": 999},
                        {"pace": 300, "distance": 1050}, {"cadence": 250}, {"distance": "1050"}):
            with self.subTest(payload=payload), self.assertRaises(MotionError):
                panel.settings_from(payload)

    def test_manual_cadence_is_preserved(self):
        self.assertEqual(panel.settings_from({"cadence": 180})["cadence"], 180)


class ControllerTests(unittest.TestCase):
    def test_map_availability_requires_a_local_image(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(panel, "ROOT", Path(directory)), \
             patch.object(panel, "RUNS", Path(directory)/"runs"), \
             patch.object(panel, "check_daily_credit"):
            controller = panel.Controller()
            self.assertFalse(controller.status()["map_available"])
            image = Path(directory)/".lab/maps/neusoft-stadium-map.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image fixture")
            self.assertTrue(controller.status()["map_available"])

    def controller(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_path = patch.object(panel, "GENERATED_STATE", Path(temporary.name)/"generated.json")
        state_path.start()
        self.addCleanup(state_path.stop)
        return panel.Controller()

    def test_cancel_preparation_requires_explicit_eligible_record_and_idle_app(self):
        controller = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run_id = "a" * 32
            path = runs / (run_id + ".json")
            base = {"state": "preparation_stopped", "segments": [], "result": None}
            with patch.object(panel, "RUNS", runs), patch.object(panel, "RunLock"), \
                 patch.object(controller.device, "activity", return_value=panel.PACKAGE + "/.mvi.home.HomeActivity"):
                for record in (base, {**base, "state": "failed", "error": "手动中止"}):
                    path.write_text(json.dumps(record))
                    controller.cancel_preparation({"run_id": run_id})
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved["state"], "cancelled_preparation")
                    self.assertIsNone(saved["result"])
                    self.assertEqual(saved["cancellation_source"], "operator_confirmed_app_home")
                    self.assertEqual(saved["app_result_status"], "unverified")
                path.write_text(json.dumps(base))
                controller.cancel_preparation({"run_id": run_id, "confirmation_source": "user_reported_no_sync_app_home_verified"})
                self.assertEqual(json.loads(path.read_text())["cancellation_source"], "user_reported_no_sync_app_home_verified")
                path.write_text(json.dumps(base))
                with self.assertRaisesRegex(MotionError, "来源无效"):
                    controller.cancel_preparation({"run_id": run_id, "confirmation_source": "unknown"})
                self.assertEqual(json.loads(path.read_text()), base)
                for change in ({"segments": [{"restoration_verified": False}]},
                               {"first_sync_confirmed_at": "confirmed"}, {"state": "running"}):
                    record = {**base, **change}
                    path.write_text(json.dumps(record))
                    with self.subTest(change=change), self.assertRaises(MotionError):
                        controller.cancel_preparation({"run_id": run_id})
                    self.assertEqual(json.loads(path.read_text()), record)
                for activity in ("RunRandomActivity", "OutdoorRunActivity"):
                    path.write_text(json.dumps(base))
                    with patch.object(controller.device, "activity", return_value=panel.PACKAGE + "/.mvi.run." + activity):
                        with self.assertRaisesRegex(MotionError, "正在跑步"):
                            controller.cancel_preparation({"run_id": run_id})
                    self.assertEqual(json.loads(path.read_text()), base)
                with patch.object(controller.device, "activity", return_value=""):
                    with self.assertRaisesRegex(MotionError, "无法核实"):
                        controller.cancel_preparation({"run_id": run_id})
                self.assertEqual(json.loads(path.read_text()), base)
                with patch.object(controller.device, "activity", return_value="com.tencent.mm/.ui.LauncherUI"):
                    with self.assertRaisesRegex(MotionError, "先回到运动世界首页"):
                        controller.cancel_preparation({"run_id": run_id})
                self.assertEqual(json.loads(path.read_text()), base)
                controller.job = {"state": "waiting"}
                with self.assertRaisesRegex(MotionError, "先停止"):
                    controller.cancel_preparation({"run_id": run_id})

    def test_cancelled_preparation_is_excluded_from_pending(self):
        controller = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            for name, state in (("cancelled", "cancelled_preparation"), ("stopped", "preparation_stopped")):
                (runs / (name + ".json")).write_text(json.dumps({"state": state, "segments": [], "result": None}))
            with patch.object(panel, "RUNS", runs), patch.object(panel, "check_daily_credit"):
                pending = controller.status()["pending"]
            self.assertEqual([record["id"] for record in pending], ["stopped"])
            self.assertTrue(pending[0]["preparation_cancellable"])

    def test_changed_settings_prevent_start_before_process_creation(self):
        controller = self.controller()
        controller.generated = {"fingerprint": {"settings": {"pace": 250}, "seed": 123}}
        with patch.object(panel, "fingerprint", return_value={"settings": {"pace": 260}, "seed": 123}), \
             patch.object(panel, "check_daily_credit"), patch.object(panel.subprocess, "Popen") as process:
            with self.assertRaisesRegex(MotionError, "重新生成"):
                controller.start("run", {"pace": 260})
            process.assert_not_called()

    def test_existing_active_job_prevents_duplicate_process(self):
        controller = self.controller()
        controller.job = {"state": "waiting"}
        with patch.object(panel.subprocess, "Popen") as process:
            with self.assertRaises(MotionError):
                controller.start("generate", {})
            process.assert_not_called()

    def test_outdoor_mode_uses_explicit_cli_and_guard(self):
        controller = self.controller()
        spec = {"settings": {"pace": 250, "cadence": 175, "distance": 1050}, "seed": 123}
        controller.generated = {"fingerprint": spec}
        with patch.object(panel, "fingerprint", return_value=spec), \
             patch.object(panel, "check_daily_credit") as guard, \
             patch.object(panel, "require_app_idle") as idle_guard, \
             patch.object(panel.subprocess, "Popen") as process, \
             patch.object(panel.threading, "Thread"):
            controller.start("run", {"mode": "outdoor"})
            guard.assert_called_once_with(mode="outdoor")
            idle_guard.assert_called_once_with(controller.device, home=True)
            command = process.call_args.args[0]
            self.assertIn("--outdoor-test", command)
            self.assertNotIn("--review-and-run", command)
            self.assertEqual(command[command.index("--seed")+1], "123")
            self.assertEqual(controller.job["mode"], "outdoor")
            self.assertTrue(controller.generated["used_for_run"])

    def test_new_generations_receive_distinct_recorded_seeds(self):
        controller = self.controller()
        with patch.object(panel.secrets, "randbits", side_effect=[11, 22]), \
             patch.object(panel, "fingerprint", side_effect=lambda settings, seed: {"settings": settings, "seed": seed}), \
             patch.object(panel.subprocess, "Popen") as process, patch.object(panel.threading, "Thread"):
            controller.start("generate", {})
            controller.job["state"] = "finished"
            controller.start("generate", {})
        seeds = [call.args[0][call.args[0].index("--seed")+1] for call in process.call_args_list]
        self.assertEqual(seeds, ["11", "22"])

    def test_fresh_input_is_rejected_while_app_run_is_still_open(self):
        controller = self.controller()
        spec = {"settings": {"pace": 250, "cadence": 175, "distance": 1050}, "seed": 123}
        controller.generated = {"fingerprint": spec}
        for name in ("RunRandomActivity", "OutdoorRunActivity"):
            with self.subTest(name=name), patch.object(panel, "fingerprint", return_value=spec), \
                 patch.object(panel, "check_daily_credit"), \
                 patch.object(controller.device, "activity", return_value=panel.PACKAGE + "/.mvi.run." + name), \
                 patch.object(panel.subprocess, "Popen") as process:
                with self.assertRaisesRegex(MotionError, "进行中的跑步"):
                    controller.start("run", {"mode": "outdoor"})
                process.assert_not_called()
                self.assertFalse(controller.generated.get("used_for_run", False))

    def test_result_answer_cannot_resolve_a_run_that_reopened_after_prompt(self):
        controller = self.controller()
        controller.job = {"id": "job", "action": "record_result", "state": "waiting",
                          "prompt": {"id": "prompt", "control": {"type": "choice", "choices":
                                     [{"label": "未产生跑步记录", "value": "未产生跑步记录"}]}}}
        controller.process = type("Process", (), {"stdin": io.StringIO()})()
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE + "/.mvi.run.OutdoorRunActivity"):
            with self.assertRaisesRegex(MotionError, "进行中的跑步"):
                controller.answer({"job_id": "job", "prompt_id": "prompt", "answer": "未产生跑步记录"})
        self.assertEqual(controller.process.stdin.getvalue(), "")
        self.assertEqual(controller.job["state"], "waiting")

    def test_pause_notice_survives_controller_reload_until_result_is_resolved(self):
        controller = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("a" * 32 + ".json")
            record = {"mode": "outdoor", "state": "failed", "phase": "awaiting_app_pause_after_failure",
                      "first_sync_confirmed_at": "confirmed", "segments": [{"restoration_verified": True}]}
            path.write_text(json.dumps(record))
            with patch.object(panel, "RUNS", Path(directory)), patch.object(panel, "check_daily_credit"):
                state = controller.status()
                self.assertTrue(state["app_pause_required"])
                self.assertTrue(state["pending"][0]["app_pause_required"])
                record.update(state="reviewed", result={"label": "已保存户外记录"})
                path.write_text(json.dumps(record))
                self.assertFalse(controller.status()["app_pause_required"])

    def test_used_plan_requires_new_generation(self):
        controller = self.controller()
        spec = {"settings": {"pace": 250}, "seed": 123}
        controller.generated = {"fingerprint": spec, "used_for_run": True}
        with patch.object(panel, "fingerprint", return_value=spec), \
             patch.object(panel, "check_daily_credit"), patch.object(panel.subprocess, "Popen") as process:
            with self.assertRaisesRegex(MotionError, "下一次节奏"):
                controller.start("run", {})
            process.assert_not_called()

    def test_invalid_seed_is_rejected_before_fingerprinting_files(self):
        for seed in (None, True, -1, 2**32, 1.2):
            with self.subTest(seed=seed), self.assertRaises(MotionError):
                panel.fingerprint({}, seed)

    def test_invalid_run_mode_is_rejected(self):
        controller = self.controller()
        with patch.object(panel.subprocess, "Popen") as process:
            with self.assertRaisesRegex(MotionError, "模式"):
                controller.start("run", {"mode": "skip"})
            process.assert_not_called()

    def test_campus_credit_and_outdoor_blocks_are_reported_separately(self):
        controller = self.controller()
        def guard(*, mode):
            if mode == "campus":
                raise MotionError("今天已有一条计分记录")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(panel, "RUNS", Path(directory)), \
             patch.object(panel, "check_daily_credit", side_effect=guard):
            state = controller.status()
        self.assertIsNotNone(state["run_blocked"])
        self.assertIsNone(state["outdoor_blocked"])

    def test_prompt_answer_is_single_use_and_rejects_multiline(self):
        controller = self.controller()
        controller.job = {"id": "job", "state": "waiting", "prompt": {"id": "prompt"}}
        class Process:
            stdin = io.StringIO()
        controller.process = Process()
        with self.assertRaises(MotionError):
            controller.answer({"job_id": "job", "prompt_id": "prompt", "answer": "one\ntwo"})
        controller.answer({"job_id": "job", "prompt_id": "prompt", "answer": "已确认"})
        self.assertEqual(controller.process.stdin.getvalue(), "已确认\n")
        with self.assertRaises(MotionError):
            controller.answer({"job_id": "job", "prompt_id": "prompt", "answer": "again"})

    def test_click_controls_reject_invalid_values_without_consuming_prompt(self):
        controls = [
            ({"type": "choice", "choices": [{"label": "继续", "value": "已确认"}]}, "yes", "已确认"),
            ({"type": "number", "min": 0, "max": 5, "step": .01}, "nan", "1.18"),
            ({"type": "number", "min": 0, "max": 5, "step": .01}, "5.01", "0"),
            ({"type": "number", "min": 0, "max": 20, "step": 1}, "1.5", "3"),
            ({"type": "time"}, "4:99", "4:10"),
        ]
        for control, invalid, valid in controls:
            with self.subTest(control=control, invalid=invalid):
                controller = self.controller()
                controller.job = {"id": "job", "state": "waiting", "prompt": {"id": "prompt", "control": control}}
                controller.process = type("Process", (), {"stdin": io.StringIO()})()
                payload = {"job_id": "job", "prompt_id": "prompt"}
                with self.assertRaises(MotionError):
                    controller.answer({**payload, "answer": invalid})
                self.assertEqual(controller.job["state"], "waiting")
                self.assertEqual(controller.process.stdin.getvalue(), "")
                controller.answer({**payload, "answer": valid})
                self.assertEqual(controller.process.stdin.getvalue(), valid+"\n")

    def test_optional_radius_can_be_skipped_without_inventing_a_value(self):
        controller = self.controller()
        controller.job = {"id": "job", "state": "waiting", "prompt": {"id": "prompt", "control":
                          {"type": "number", "min": 1, "max": 100, "step": 1, "optional": True}}}
        controller.process = type("Process", (), {"stdin": io.StringIO()})()
        controller.answer({"job_id": "job", "prompt_id": "prompt", "answer": ""})
        self.assertEqual(controller.process.stdin.getvalue(), "\n")

    def test_app_gps_block_does_not_consume_start_prompt_or_create_a_failed_job(self):
        controller = self.controller()
        controller.job = {"id":"job", "state":"waiting", "prompt":{"id":"prompt", "control":{
            "type":"choice", "choices":[{"label":"开始同步", "value":""}], "requires_app_run_mode":"campus"}}}
        controller.process = type("Process", (), {"stdin":io.StringIO()})()
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE+"/.mvi.home.HomeActivity"):
            with self.assertRaisesRegex(MotionError, "GPS信号弱"):
                controller.answer({"job_id":"job", "prompt_id":"prompt", "answer":""})
        self.assertEqual(controller.job["state"], "waiting")
        self.assertEqual(controller.job["prompt"]["id"], "prompt")
        self.assertEqual(controller.process.stdin.getvalue(), "")
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE+"/.mvi.run.RunRandomActivity"):
            controller.answer({"job_id":"job", "prompt_id":"prompt", "answer":""})
        self.assertEqual(controller.job["state"], "running")
        self.assertEqual(controller.process.stdin.getvalue(), "\n")

    def test_wrong_app_run_mode_is_rejected_before_acknowledgement(self):
        controller = self.controller()
        controller.job = {"id":"job", "state":"waiting", "prompt":{"id":"prompt", "control":{
            "type":"choice", "choices":[{"label":"开始同步", "value":""}], "requires_app_run_mode":"campus"}}}
        controller.process = type("Process", (), {"stdin":io.StringIO()})()
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE+"/.mvi.run.OutdoorRunActivity"):
            with self.assertRaises(MotionError):
                controller.answer({"job_id":"job", "prompt_id":"prompt", "answer":""})
        self.assertEqual(controller.job["state"], "waiting")
        self.assertEqual(controller.process.stdin.getvalue(), "")

    def test_home_requirement_rejects_other_pages_without_consuming_prompt(self):
        controller = self.controller()
        controller.job = {"id": "job", "state": "waiting", "prompt": {"id": "prompt", "control": {
            "type": "choice", "choices": [{"label": "开始预热", "value": ""}], "requires_app_home": True}}}
        controller.process = type("Process", (), {"stdin": io.StringIO()})()
        payload = {"job_id": "job", "prompt_id": "prompt", "answer": ""}
        for activity in (panel.PACKAGE + "/.mvi.run.OutdoorRunActivity",
                         panel.PACKAGE + "/.mvi.run.RunRandomActivity",
                         panel.PACKAGE + "/.mvi.run.RunHistoryDetailActivity",
                         "other.package/.mvi.home.HomeActivity", ""):
            with self.subTest(activity=activity), patch.object(controller.device, "activity", return_value=activity) as current:
                with self.assertRaises(MotionError):
                    controller.answer(payload)
                current.assert_called_once_with()
                self.assertEqual(controller.process.stdin.getvalue(), "")
                self.assertEqual(controller.job["state"], "waiting")
                self.assertEqual(controller.job["prompt"]["id"], "prompt")
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE + "/.mvi.home.HomeActivity") as current:
            controller.answer(payload)
            current.assert_called_once_with()
        self.assertEqual(controller.process.stdin.getvalue(), "\n")
        self.assertEqual(controller.job["state"], "running")
        self.assertIsNone(controller.job["prompt"])

    def test_process_forwards_structured_choices_to_page(self):
        controller = self.controller()
        control = {"type": "choice", "choices": [{"label": "App 已暂停", "value": "已暂停"}]}
        class Process:
            stdin = io.StringIO()
            def __init__(self):
                self.stdout = self.messages()
            def messages(self):
                yield json.dumps({"kind": "prompt", "text": "确认已暂停", "control": control})+"\n"
                self.captured = dict(controller.job["prompt"])
            def wait(self):
                return 0
        process = Process()
        # The reader closes its stream; this test double implements that operation.
        class Stream:
            def __iter__(self):
                return process.messages()
            def close(self):
                pass
        process.stdout = Stream()
        controller.job = {"state": "running", "events": []}
        controller.read_process(process, controller.job, None, None)
        self.assertEqual(process.captured["control"], control)
        self.assertEqual(process.captured["text"], "确认已暂停")

    def test_foreground_privacy_gate_prevents_screenshot(self):
        controller = self.controller()
        with patch.object(controller.device, "activity", return_value="com.tencent.mm/.SomeActivity"), \
             patch.object(panel.subprocess, "run") as run:
            with self.assertRaises(MotionError):
                controller.snapshot()
            run.assert_not_called()

    def test_expired_wechat_is_reported_without_capturing_it(self):
        controller = self.controller()
        with patch.object(controller.device, "activity", return_value="com.tencent.mm/.ui.AccountExpiredUI"), patch.object(panel.subprocess, "run") as run:
            with self.assertRaisesRegex(MotionError, "微信登录已过期"):
                controller.snapshot()
            self.assertEqual(controller.device_status, "wechat_expired")
            run.assert_not_called()

    def test_running_input_prevents_app_relaunch(self):
        controller = self.controller()
        controller.job = {"action": "run", "state": "running"}
        with patch.object(controller.device, "adb") as adb:
            with self.assertRaises(MotionError):
                controller.open_app()
            adb.assert_not_called()

    def test_stale_frame_and_outside_coordinates_rejected(self):
        controller = self.controller()
        frame = {"id": "frame", "width": 640, "height": 1280, "at": time.monotonic(), "activity": panel.PACKAGE}
        controller.frames.append(frame)
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE), patch.object(controller.device, "adb") as adb:
            with self.assertRaises(MotionError):
                controller.interact({"kind": "tap", "frame_id": "frame", "x": 640, "y": 10})
            frame["at"] -= 16
            with self.assertRaises(MotionError):
                controller.interact({"kind": "tap", "frame_id": "frame", "x": 10, "y": 10})
            adb.assert_not_called()

    def test_direct_user_gesture_uses_bounded_arguments(self):
        controller = self.controller()
        controller.frames.append({"id": "frame", "width": 640, "height": 1280, "at": time.monotonic(), "activity": panel.PACKAGE})
        with patch.object(controller.device, "activity", return_value=panel.PACKAGE), patch.object(controller.device, "adb") as adb:
            controller.interact({"kind": "swipe", "frame_id": "frame", "x": 639.9, "y": 10,
                                 "end_x": 600, "end_y": 800, "duration_ms": 2500})
            adb.assert_called_once_with("shell", "input", "swipe", "639", "10", "600", "800", "2500", timeout=5)

    def test_telemetry_is_preserved_separately_from_logs(self):
        controller = self.controller()
        messages = [{"kind": "telemetry", "data": {"phase": "sending", "distance_m": 40, "elapsed_s": 10}},
                    {"kind": "telemetry", "data": {"phase": "restored", "restoration_verified": True}}]
        class Process:
            stdout = io.StringIO("\n".join(json.dumps(message) for message in messages)+"\n")
            stdin = io.StringIO()
            def wait(self):
                return 0
        job = {"state": "running", "events": [], "prompt": None, "telemetry": None, "telemetry_history": []}
        controller.read_process(Process(), job, None, None)
        self.assertEqual(job["telemetry"]["distance_m"], 40)
        self.assertEqual(job["telemetry"]["phase"], "restored")
        self.assertEqual(len(job["telemetry_history"]), 1)
        self.assertEqual(job["telemetry_history"][0]["phase"], "sending")
        self.assertEqual(job["events"], [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        class Controller:
            def status(self):
                return {"offline_fixture": True}
            def start(self, action, payload):
                return "offline-job"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), panel.make_handler(Controller(), "test-token", "test-instance"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_unauthenticated_access_denied(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base+"/api/status")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_bootstrap_query_replaces_a_stale_cookie(self):
        request = Request(self.base+"/?token=test-token", headers={"Cookie": "campus_control=stale"})
        with urlopen(request) as response:
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
            self.assertIn("校园跑控制台", response.read().decode())

    def test_foreign_origin_cannot_control_device(self):
        request = Request(self.base+"/api/action", data=b'{"action":"generate"}',
                          headers={"Cookie": "campus_control=test-token", "Origin": "https://example.org", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_authorized_local_request_and_static_path_allowlist(self):
        request = Request(self.base+"/api/action", data=b'{"action":"generate"}',
                          headers={"Cookie": "campus_control=test-token", "Origin": self.base, "Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["job_id"], "offline-job")
        request = Request(self.base+"/../private/session-status.json", headers={"Cookie": "campus_control=test-token"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
