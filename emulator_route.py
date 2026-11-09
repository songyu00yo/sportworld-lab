#!/usr/bin/env python3
"""Validate an explicitly supplied closed WGS84 polyline; optionally send AVD inputs.

JSON: {"closed": true, "points": [[longitude, latitude], ...]}.
Vertices must be distinct; the closing edge is implicit. No route is generated.
This module neither interacts with apps nor verifies exercise or scores.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import secrets
import sys
import time

from emulator_motion import Console, MotionError, bounded, restore, set_values, values

PORT = 15556
AVD = "sportworld_tablet34"
STADIUM_SEED = (121.529540, 38.894502)
EARTH_RADIUS = 6371008.8


def metres(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(min(1, math.sqrt(h)))


def validate_route(data):
    if not isinstance(data, dict) or data.get("closed") is not True:
        raise MotionError("Route must explicitly declare closed=true")
    points = data.get("points")
    if not isinstance(points, list) or not 3 <= len(points) <= 1000:
        raise MotionError("Expected 3 to 1000 distinct vertices")
    vertices = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) for v in point):
            raise MotionError("Each vertex must contain two finite numeric coordinates")
        lon, lat = point
        if not -180 <= lon <= 180 or not -90 <= lat <= 90 or not all(math.isfinite(v) for v in point):
            raise MotionError("WGS84 coordinate out of range")
        vertex = (float(lon), float(lat))
        if metres(STADIUM_SEED, vertex) > 600:
            raise MotionError("Vertex exceeds the 600m stadium boundary")
        if vertex in vertices:
            raise MotionError("Repeated vertices are forbidden; closure is implicit")
        vertices.append(vertex)
    lengths = [metres(vertices[i], vertices[(i + 1) % len(vertices)]) for i in range(len(vertices))]
    if any(length <= 0 or length > 80 for length in lengths):
        raise MotionError("Every edge, including closure, must be longer than 0 and at most 80m")
    return vertices, lengths


def load_route(path):
    if path.stat().st_size > 256 * 1024:
        raise MotionError("Route JSON exceeds 256KiB")
    def invalid_constant(value):
        raise MotionError("Non-finite JSON number is forbidden")
    return validate_route(json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant))


def position(vertices, lengths, distance):
    if not math.isfinite(distance) or distance < 0:
        raise MotionError("Arc distance must be finite and nonnegative")
    remaining = distance % sum(lengths)
    for i, length in enumerate(lengths):
        if remaining < length or i == len(lengths) - 1:
            fraction = min(1, remaining / length)
            a, b = vertices[i], vertices[(i + 1) % len(vertices)]
            return tuple(a[j] + (b[j] - a[j]) * fraction for j in range(2))
        remaining -= length
    raise MotionError("Cannot interpolate route")


def plan_duration(distance, pace):
    duration = distance * pace / 1000
    if not math.isfinite(duration) or not 0 < duration <= 300:
        raise MotionError("Planned input duration must be greater than 0 and at most 300s")
    return duration


def identify(console):
    if console.command("avd name") != AVD:
        raise MotionError("Refusing a device other than the isolated tablet AVD")


def nmea_coordinate(value, degree_digits):
    # Round the complete angle first so 59.9999999 minutes carries to degrees.
    total = round(abs(value) * 60 * 1_000_000)
    degrees, minutes = divmod(total, 60 * 1_000_000)
    whole, fraction = divmod(minutes, 1_000_000)
    return f"{degrees:0{degree_digits}d}{whole:02d}.{fraction:06d}"


def nmea_sentence(body):
    checksum = 0
    for value in body.encode("ascii"):
        checksum ^= value
    return f"${body}*{checksum:02X}"


def gps(console, point, speed_knots, bearing_degrees=0.0, altitude_m=50.0):
    if not all(math.isfinite(v) for v in (*point, speed_knots, bearing_degrees, altitude_m)):
        raise MotionError("Non-finite GPS input")
    longitude, latitude = point
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90 or speed_knots < 0:
        raise MotionError("GPS coordinate or speed outside valid range")
    transport = os.environ.get("SPORTWORLD_GPS_TRANSPORT", "nmea")
    if transport == "grpc":
        from emulator_gps import CachedGps
        if not hasattr(console, "cached_gps"):
            console.cached_gps = CachedGps(AVD, PORT)
        console.cached_gps.update(point, speed_knots, bearing_degrees, altitude_m)
        return
    if transport != "nmea":
        raise MotionError("Unknown GPS transport")
    stamp = datetime.now(timezone.utc)
    clock = stamp.strftime("%H%M%S")
    lat, lon = nmea_coordinate(latitude, 2), nmea_coordinate(longitude, 3)
    ns, ew = ("S" if latitude < 0 else "N"), ("W" if longitude < 0 else "E")
    position = f"{lat},{ns},{lon},{ew}"
    # GGA updates satellite status; RMC then publishes the corresponding fix.
    # Raw NMEA requires the isolated emulator to use -no-passive-gps.
    gga = f"GPGGA,{clock},{position},1,10,1.0,{altitude_m},M,0.0,M,,"
    rmc = f"GPRMC,{clock},A,{position},{speed_knots:.9f},{bearing_degrees % 360:.9f},{stamp.strftime('%d%m%y')},0.0,W"
    for body in (gga, rmc):
        console.command("geo nmea " + nmea_sentence(body))


class StopRequested(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


def request_stop(signum, frame):
    raise StopRequested(signum)


def cleanup(last_point, original):
    """Never reuse an interrupted command stream, and attempt both restorations."""
    errors = []
    if last_point is not None:
        fresh = None
        try:
            fresh = Console(PORT)
            identify(fresh)
            gps(fresh, last_point, 0)
        except Exception as error:
            errors.append("GPS stop: " + str(error))
        finally:
            if fresh is not None:
                try:
                    fresh.close()
                except OSError as error:
                    errors.append("GPS connection close: " + str(error))
    try:
        restore(PORT, AVD, original)
    except Exception as error:
        errors.append("sensor restoration: " + str(error))
    if errors:
        raise MotionError("CLEANUP FAILED: " + "; ".join(errors))


def execute(vertices, lengths, args):
    duration = plan_duration(args.distance, args.pace)
    speed = 1000 / args.pace
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in handlers:
        signal.signal(sig, request_stop)
    console = None
    original = None
    last_point = None
    sent_distance = 0.0
    interrupted = 0
    try:
        console = Console(PORT)
        identify(console)
        original = {name: values(console, name) for name in ("acceleration", "gyroscope")}
        gravity = math.sqrt(sum(v * v for v in original["acceleration"]))
        if gravity < 0.01:
            raise MotionError("Acceleration baseline has no gravity direction")
        direction = tuple(v / gravity for v in original["acceleration"])
        started = time.monotonic()
        next_gps, next_progress, tick = 0.0, 15.0, 0
        while True:
            elapsed = min(duration, time.monotonic() - started)
            distance = min(args.distance, elapsed * speed)
            if elapsed >= next_gps or elapsed >= duration:
                # Remember an attempted write too: interruption may occur after send.
                last_point = position(vertices, lengths, distance)
                gps(console, last_point, speed / (1852 / 3600))
                sent_distance = distance
                next_gps = math.floor(elapsed) + 1
            if elapsed >= duration:
                break
            phase = elapsed * args.cadence / 60 * 2 * math.pi
            wave = math.sin(phase)
            set_values(console, "acceleration", tuple(v + d * 1.5 * wave for v, d in zip(original["acceleration"], direction)))
            set_values(console, "gyroscope", tuple(v + (0.15 * wave if i == 2 else 0) for i, v in enumerate(original["gyroscope"])))
            if elapsed >= next_progress:
                print(f"Synthetic GPS inputs sent: distance={sent_distance:.1f}m elapsed={elapsed:.1f}s", flush=True)
                next_progress = (math.floor(elapsed / 15) + 1) * 15
            tick += 1
            wait = min(started + duration, started + tick / 20) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            else:
                tick = math.floor((time.monotonic() - started) * 20)
    except StopRequested as error:
        interrupted = 128 + error.signum
    except KeyboardInterrupt:
        interrupted = 130
    finally:
        # Cleanup is bounded by Console timeouts and cannot be interrupted by a
        # second SIGINT/SIGTERM; OS termination or device loss can still prevent it.
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if console is not None:
                try:
                    console.close()
                except OSError:
                    pass
            if original is not None:
                cleanup(last_point, original)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
    if original is not None:
        print(f"Synthetic input stopped at sent distance={sent_distance:.1f}m; last attempted GPS point stopped and sensors restored. Inputs only, not a score.")
    else:
        print("Stopped before input initialization. No score verification was performed.")
    return interrupted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", type=Path)
    parser.add_argument("--profile", type=Path, help="Pinned MotionSense template JSON")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--run", action="store_true", help="Explicitly send synthetic inputs to the isolated AVD")
    execution.add_argument("--outdoor-test", action="store_true", help="Bounded outdoor-mode measurement in the isolated AVD")
    execution.add_argument("--check", action="store_true", help="Geometry and timing validation only; no app acceptance check")
    execution.add_argument("--prepare-reference", action="store_true", help="Download and verify the pinned public reference")
    execution.add_argument("--verify", action="store_true", help="Offline coupled-motion and held-out-reference report")
    execution.add_argument("--export", type=Path, help="Export offline samples and a JSON report to new files")
    execution.add_argument("--review-route", action="store_true", help="Manually confirm today's app-issued checkpoints")
    execution.add_argument("--review-and-run", action="store_true", help="Confirm checkpoints, then enter the guarded manual run flow")
    execution.add_argument("--record-result", type=Path, help="Record the actual app result for a pending run")
    execution.add_argument("--guided", action="store_true", help="Chinese guided entry point")
    parser.add_argument("--distance", type=bounded(1, 1200), default=1050.0)
    parser.add_argument("--pace", type=bounded(200, 600), default=250.0, help="Seconds per kilometre")
    parser.add_argument("--cadence", type=bounded(80, 240), default=175.0)
    parser.add_argument("--seed", type=int, help="Reproducible pacing seed; omitted values generate a fresh rhythm")
    args = parser.parse_args(argv)
    if args.outdoor_test:
        args.run = True
    args.mode = "outdoor" if args.outdoor_test else "campus"
    try:
        if args.seed is None:
            args.seed = secrets.randbits(32)
        if not 0 <= args.seed < 2**32:
            raise MotionError("节奏编号应为 0 到 4294967295 的整数")
        if args.prepare_reference:
            from motion_reference import prepare_template
            path, manifest = prepare_template()
            print(f"公开参考数据已校验，模板保存在 {path}")
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0
        if args.record_result:
            from run_session import RunLock, record_result
            with RunLock():
                record_result(args.record_result)
            return 0
        if args.route is None:
            raise MotionError("请提供 --route 路线文件")
        if args.review_and_run:
            from run_session import check_daily_credit, review_route, RunLock
            with RunLock():
                check_daily_credit()
                review_route(args.route, json.loads(args.route.read_text()))
            args.run = True
        if args.guided:
            print("1  离线检查\n2  核对当天点位并准备输入\n3  登记上次 App 结果\n0  退出")
            choice = input("输入编号后按回车\n").strip()
            if choice == "0":
                return 0
            if choice == "3":
                from run_session import RUNS
                pending = [p for p in sorted(RUNS.glob("*.json")) if json.loads(p.read_text()).get("state") not in ("reviewed", "accepted", "cancelled_preparation")]
                if not pending:
                    print("没有待核验记录。")
                    return 0
                for i, path in enumerate(pending, 1):
                    print(i, path.name)
                selected = int(input("选择记录编号\n"))
                if not 1 <= selected <= len(pending):
                    raise MotionError("记录编号无效")
                return main(["--record-result", str(pending[selected-1])])
            if choice == "1":
                args.verify = True
            elif choice == "2":
                from run_session import check_daily_credit, review_route, RunLock
                with RunLock():
                    check_daily_credit()
                    review_route(args.route, json.loads(args.route.read_text()))
                args.run = True
            else:
                raise MotionError("请输入菜单中的编号")
        vertices, lengths = load_route(args.route)
        duration = plan_duration(args.distance, args.pace)
        print(f"Geometry valid: {len(vertices)} vertices, loop={sum(lengths):.1f}m, target={args.distance:g}m, duration={duration:g}s; no app or score verification.")
        if args.review_route:
            from run_session import RunLock, review_route
            with RunLock():
                review_route(args.route, json.loads(args.route.read_text()))
            return 0
        if not (args.run or args.verify or args.export):
            print("Validation only; no emulator connection or input was made.")
            return 0
        from motion_reference import REFERENCE_DIR, Reference
        from run_trajectory import build_samples
        profile = args.profile or REFERENCE_DIR / "template.json"
        reference = Reference(profile)
        data = json.loads(args.route.read_text())
        if args.run:
            os.environ.setdefault("SPORTWORLD_GPS_TRANSPORT", "grpc")
            from run_session import interactive_run
            return interactive_run(args.route, data, reference, args)
        from motion_report import make_report, export_samples
        samples, metadata = build_samples(data, reference, args.distance, args.pace, args.cadence, seed=args.seed)
        report = make_report(samples, metadata, reference)
        if args.export:
            path = export_samples(args.export, samples, report)
            print(f"离线输入已导出到 {args.export}\n检查报告保存在 {path}")
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (MotionError, OSError, ValueError, KeyError, TypeError, EOFError) as error:
        print("Route input failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if "--ui-protocol" in sys.argv:
        sys.argv.remove("--ui-protocol")
        from ui_protocol import run
        sys.exit(run(main))
    sys.exit(main())
