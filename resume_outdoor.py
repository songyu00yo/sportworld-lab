"""Manually resume one interrupted outdoor test without creating another run."""
import argparse
import hashlib
import json
import math
import os
import re
import signal
import sys

from emulator_motion import Console, MotionError, values
from emulator_route import identify, metres
from motion_reference import ROOT, REFERENCE_DIR, Reference
from motion_report import expected_timeline
from run_trajectory import build_samples, verify_samples
from run_session import (RUNS, RunLock, Device, BootMonitor, StartLocationKeepalive,
                         atomic_json, interruptible, now, numeric_input, record_result,
                         recover, running_activity_matches, transmit)
from run_session import gps_anchor_offset, prepare_gps_channel
from ui_protocol import publish_plan, publish_progress


def remaining_plan(state, data, reference, baseline):
    segments = state.get("segments", [])
    if state.get("mode") != "outdoor" or state.get("state") != "failed" or not segments:
        raise MotionError("仅能继续已中断且尚未结束的户外测试")
    if not all(s.get("restoration_verified") is True for s in segments):
        raise MotionError("上次传感器恢复尚未确认，不能继续")
    last = segments[-1]
    gps_offset = last.get("gps_offset_degrees", (0.0, 0.0))
    if (not isinstance(gps_offset, (tuple, list)) or len(gps_offset) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in gps_offset)):
        raise MotionError("中断记录的 GPS 观测偏移无效")
    plan = last["planned"]
    source, _ = build_samples(data, reference, plan["distance_m"], plan["pace_target_s_km"],
                              plan["cadence_target"], offset=plan["offset_m"],
                              phase=plan.get("phase_start_s", 0), gravity=plan["gravity_baseline"],
                              allow_slower=True, seed=state["pace_profile_seed"],
                              elapsed_offset_s=plan.get("elapsed_offset_s", 0),
                              model_version=plan.get("motion_model_version", 1),
                              elevation=plan.get("elevation_profile"),
                              magnetic_field=plan.get("magnetic_field_baseline", (0, 9.88766, -47.7452)),
                              magnetic_field_world=plan.get("magnetic_field_world"))
    limit = float(last["sent_distance_m"])
    # Only the latest GPS interval can be the anchor; older laps revisit the
    # same coordinates and must never be mistaken for the interruption point.
    candidates = [s for s in source if max(0, limit - 12) <= s["distance_m"] <= limit + 1e-6]
    if not candidates or not last.get("last_point"):
        raise MotionError("没有可核对的中断位置")
    anchor = min(candidates, key=lambda s: metres((s["longitude"] + gps_offset[0], s["latitude"] + gps_offset[1]), last["last_point"]))
    observed_anchor = (anchor["longitude"] + gps_offset[0], anchor["latitude"] + gps_offset[1])
    gps_anchor_offset(observed_anchor, anchor)
    if metres(observed_anchor, last["last_point"]) > .05:
        raise MotionError("中断位置与原计划不符")
    offset = anchor["arc_m"]
    target = segments[0]["planned"]["offset_m"] + segments[0]["planned"]["distance_m"]
    remaining = target - offset
    used_s = sum(float(s["input_elapsed_s"]) for s in segments)
    used_m = sum(float(s["sent_distance_m"]) for s in segments)
    if not all(math.isfinite(v) for v in (remaining, used_s, used_m)) or remaining <= 0:
        raise MotionError("剩余距离或累计输入记录无效")
    samples, metadata = build_samples(data, reference, remaining, plan["pace_target_s_km"],
                                     plan["cadence_target"], offset=offset, phase=anchor["phase_s"],
                                     gravity=baseline["acceleration"], allow_slower=True,
                                     seed=state["pace_profile_seed"],
                                     elapsed_offset_s=anchor.get("absolute_time_s", used_s),
                                     model_version=plan.get("motion_model_version", 1),
                                     elevation=plan.get("elevation_profile"),
                                     magnetic_field=plan.get("magnetic_field_baseline", (0, 9.88766, -47.7452)),
                                     magnetic_field_world=plan.get("magnetic_field_world"))
    if used_s + metadata["duration_s"] > 300 or used_m + remaining > 1200:
        raise MotionError("继续会超过累计 1.2 公里或 300 秒限制")
    metadata["phase_start_s"] = anchor["phase_s"]
    verify_samples(samples, metadata)
    return samples, metadata, used_s, used_m


def resume(run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise MotionError("运行记录编号无效")
    path = RUNS / (run_id + ".json")
    route = ROOT / ".lab/routes/neusoft-track-route.json"
    with RunLock(), interruptible():
        state = json.loads(path.read_text())
        if any(p != path and json.loads(p.read_text()).get("state") not in ("reviewed", "accepted", "cancelled_preparation")
               for p in RUNS.glob("*.json")):
            raise MotionError("还有其他运行记录需要先核对")
        reference = Reference(REFERENCE_DIR / "template.json")
        if hashlib.sha256(route.read_bytes()).hexdigest() != state["route_sha256"] or reference.sha256 != state["reference_sha256"]:
            raise MotionError("路线或动作参考已变化，不能续用旧记录")
        device = Device()
        readiness = device.preflight()
        if readiness["boot_id"] != state["readiness"]["boot_id"]:
            raise MotionError("模拟器已重启，不能继续上次测试")
        if not running_activity_matches(device.activity(), "outdoor"):
            raise MotionError("请先回到尚未结束的本次户外跑页面")
        connection = Console(15556)
        try:
            identify(connection)
            from sensor_channels import COUPLED_SENSORS
            sensors = COUPLED_SENSORS if state["segments"][-1]["planned"].get("motion_model_version", 1) >= 2 else ("acceleration", "gyroscope")
            baseline = {k: values(connection, k) for k in sensors}
        finally:
            connection.close()
        if any(abs(v) > .1 for v in baseline["gyroscope"]):
            raise MotionError("传感器尚未静止")
        samples, metadata, used_s, used_m = remaining_plan(state, json.loads(route.read_text()), reference, baseline)
        from ui_protocol import choose, acknowledge
        if choose("确认同一次户外跑已暂停且尚未结束，输入 已暂停\n", [("同一次跑步已暂停，继续", "已暂停")],
                  text="确认右侧 App 仍是上次户外跑，已暂停且尚未结束。").strip() != "已暂停":
            raise MotionError("尚未确认本次 App 进度")
        publish_plan({"segment_index": len(state["segments"]), "timeline": expected_timeline(samples, used_s)})
        print(f"将从 {metadata['offset_m']:.1f} 米继续，补足 {metadata['distance_m']:.1f} 米。")
        gps_offset = state["segments"][-1].get("gps_offset_degrees", (0.0, 0.0))
        start_point = (samples[0]["longitude"] + gps_offset[0], samples[0]["latitude"] + gps_offset[1])
        with StartLocationKeepalive(device, start_point,
                                    altitude_m=samples[0].get("altitude_m"), app_run_mode="outdoor"):
            acknowledge("在 App 点击继续，倒计时结束后按回车\n", "倒计时已结束，继续同步",
                        text="先在右侧 App 点击继续，倒计时结束时点击下方按钮。", app_run_mode="outdoor")
        if not running_activity_matches(device.activity(), "outdoor"):
            raise MotionError("App 已离开本次户外跑")
        state.setdefault("interruptions", []).append({"at": now().isoformat(), "error": state.get("error"), "offset_m": metadata["offset_m"]})
        metrics = {"frames": 0, "missed_frames": 0, "gps_samples": 0, "max_lateness_s": 0,
                   "max_frame_gap_s": 0, "input_elapsed_s": 0, "sent_distance_m": 0,
                   "last_point": start_point, "planned": metadata, "elapsed_before_segment_s": used_s,
                   "gps_offset_degrees": gps_offset,
                   "gps_anchor_adjustment_m": metres(start_point, (samples[0]["longitude"], samples[0]["latitude"])),
                   "acknowledged_at": now().isoformat(), "resume": True}
        if "altitude_m" in samples[0]:
            metrics["last_altitude_m"] = samples[0]["altitude_m"]
        state["segments"].append(metrics)
        state["state"] = "running"
        state["phase"] = "input"
        state["app_session_unresolved"] = False
        state.pop("error", None)
        atomic_json(path, state)
        connection = None
        try:
            try:
                connection = Console(15556)
                identify(connection)
                prepare_gps_channel(connection)
                with BootMonitor(device) as monitor:
                    transmit(samples, connection, monitor, metrics)
            finally:
                handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
                metrics["restoration_verified"] = False
                try:
                    try:
                        if connection is not None:
                            connection.close()
                    finally:
                        if "last_altitude_m" in metrics:
                            recover(device, metrics["last_point"], baseline, last_altitude_m=metrics["last_altitude_m"])
                        else:
                            recover(device, metrics["last_point"], baseline)
                        metrics["restoration_verified"] = True
                        publish_progress({"phase": "restored", "restoration_verified": True})
                finally:
                    atomic_json(path, state)
                    for sig, handler in handlers.items():
                        signal.signal(sig, handler)
            state["phase"] = "awaiting_app_pause"
            atomic_json(path, state)
            acknowledge("输入已停止并恢复传感器。在 App 暂停后按回车\n", "App 已暂停，继续",
                        text="输入已停止并恢复传感器，请先在右侧 App 暂停跑步。")
            state["input_distance_m"] = used_m + metadata["distance_m"]
            state["input_duration_s"] = used_s + metrics["input_elapsed_s"]
            from ui_protocol import web_active
            if not web_active():
                metrics["app_distance_km_at_pause"] = numeric_input("App 当前显示距离，单位公里", 0, 5)
            state["state"] = "review_pending"
            atomic_json(path, state)
            if web_active():
                print("补足流程已完成，请在 App 结束本次跑步。结果可在运行记录中补记，无需填写即可返回。")
            else:
                record_result(path)
        except (Exception, KeyboardInterrupt) as error:
            state["state"] = ("review_pending" if state.get("phase") == "awaiting_app_pause"
                              and all(segment.get("restoration_verified") is True for segment in state["segments"])
                              else "failed")
            if state["state"] == "failed":
                state["phase"] = "awaiting_app_pause_after_failure"
                state["app_session_unresolved"] = True
            state["error"] = str(error) or "手动中止"
            atomic_json(path, state)
            raise
    return 0


def main():
    os.environ.setdefault("SPORTWORLD_GPS_TRANSPORT", "grpc")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        return resume(args.run_id)
    except (MotionError, OSError, ValueError, KeyError, EOFError) as error:
        print("继续已停止：" + str(error))
        return 1


if __name__ == "__main__":
    if "--ui-protocol" in sys.argv:
        sys.argv.remove("--ui-protocol")
        from ui_protocol import run
        sys.exit(run(main))
    sys.exit(main())
