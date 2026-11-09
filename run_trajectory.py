"""Offline route, time law and coupled specific-force/attitude simulation."""
import bisect
import json
import math
import random
import statistics

from emulator_motion import MotionError
from emulator_route import EARTH_RADIUS, validate_route
from motion_reference import (G, align_vectors, angular_velocity, axis_angle,
                              conjugate, norm, qmul, rotate, slerp, unwrap)

DEFAULT_GRAVITY = (0.0, 9.77631, 0.812349)
DEFAULT_MAGNETIC_FIELD = (0.0, 9.88766, -47.7452)
HZ = 20
MOTION_MODEL_VERSION = 4
GAIT_FILTER_CUTOFF_HZ = 4.0
GAIT_FILTER_TAPS = 41
MAX_MODEL_STEP_LENGTH_M = 2.1


def blend(a, b, fraction):
    return tuple(x+(y-x)*fraction for x, y in zip(a, b))


def cubic(a, b, c, d, t):
    u = 1-t
    point = tuple(u**3*a[i]+3*u*u*t*b[i]+3*u*t*t*c[i]+t**3*d[i] for i in range(2))
    first = tuple(3*u*u*(b[i]-a[i])+6*u*t*(c[i]-b[i])+3*t*t*(d[i]-c[i]) for i in range(2))
    second = tuple(6*u*(c[i]-2*b[i]+a[i])+6*t*(d[i]-2*c[i]+b[i]) for i in range(2))
    speed = norm(first)
    if speed < 1e-9:
        raise MotionError("Route contains a cusp; adjust the route geometry")
    curvature = (first[0]*second[1]-first[1]*second[0])/speed**3
    return point, math.atan2(first[1], first[0]), curvature


class SmoothPath:
    def __init__(self, data):
        vertices, lengths = validate_route(data)
        self.origin = vertices[0]
        self.lon_scale = EARTH_RADIUS*math.cos(math.radians(self.origin[1]))*math.pi/180
        self.lat_scale = EARTH_RADIUS*math.pi/180
        points = [((lon-self.origin[0])*self.lon_scale, (lat-self.origin[1])*self.lat_scale)
                  for lon, lat in vertices]
        tangents = []
        for i, point in enumerate(points):
            previous, following = points[i-1], points[(i+1) % len(points)]
            direction = tuple(following[j]-previous[j] for j in range(2))
            size = norm(direction)
            if size < 1e-6:
                raise MotionError("Route makes an unsupported reversal")
            handle = min(4.0, lengths[i-1]/4, lengths[i]/4)
            tangents.append(tuple(value*handle/size for value in direction))
        self.distances, self.points, self.headings, self.curvatures = [], [], [], []
        self.vertex_distances = []
        for i, a in enumerate(points):
            d = points[(i+1) % len(points)]
            b = tuple(a[j]+tangents[i][j] for j in range(2))
            c = tuple(d[j]-tangents[(i+1) % len(points)][j] for j in range(2))
            steps = max(20, math.ceil(lengths[i]/.2))
            self.vertex_distances.append(self.distances[-1] if self.distances else 0)
            for step in range(steps+1):
                if i and step == 0:
                    continue
                point, heading, curvature = cubic(a, b, c, d, step/steps)
                distance = self.distances[-1]+norm(tuple(x-y for x, y in zip(point, self.points[-1]))) if self.points else 0.0
                self.distances.append(distance)
                self.points.append(point)
                self.headings.append(heading)
                self.curvatures.append(curvature)
        self.headings = unwrap(self.headings)
        self.length = self.distances[-1]
        self.heading_winding = self.headings[-1]-self.headings[0]

    def at(self, distance):
        lap = math.floor(distance/self.length)
        offset = distance-lap*self.length
        upper = min(len(self.distances)-1, max(1, bisect.bisect_right(self.distances, offset)))
        lower = upper-1
        ratio = (offset-self.distances[lower])/(self.distances[upper]-self.distances[lower])
        point = blend(self.points[lower], self.points[upper], ratio)
        heading = self.headings[lower]+ratio*(self.headings[upper]-self.headings[lower])+lap*self.heading_winding
        curvature = self.curvatures[lower]+ratio*(self.curvatures[upper]-self.curvatures[lower])
        return point, heading, curvature

    def coordinates(self, point):
        return (self.origin[0]+point[0]/self.lon_scale, self.origin[1]+point[1]/self.lat_scale)


class TimeLaw:
    """Arc-length speed caps with forward/backward longitudinal limits."""
    def __init__(self, path, distance, pace, offset=0.0, allow_slower=False, seed=0, *, profile_version=2):
        self.duration = distance*pace/1000
        if not all(math.isfinite(v) for v in (distance, pace, offset)) or not 0 < distance <= 1200 or not 0 < self.duration <= 300 or offset < 0:
            raise MotionError("Distance/time budget exceeds 1200 m or 300 seconds")
        count = math.ceil(distance/.2)
        knots = [distance*i/count for i in range(count+1)]
        # Include every geometric knot, including short high-curvature corners.
        # Limit both ends of an interval by its worst neighboring curvature.
        for lap in range(math.floor(offset/path.length), math.ceil((offset+distance)/path.length)+1):
            knots.extend(lap*path.length+s-offset for s in path.distances if 0 < lap*path.length+s-offset < distance)
        self.distances = [0.0]
        for value in sorted(knots):
            if value-self.distances[-1] > 1e-6 and distance-value > 1e-6:
                self.distances.append(value)
        self.distances.append(distance)
        curvatures = [abs(path.at(offset+s)[2]) for s in self.distances]
        envelope = [max(curvatures[max(0, i-1):i+2]) for i in range(len(curvatures))]
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise MotionError("Pace profile seed must be an integer")
        if profile_version not in (1, 2):
            raise MotionError("Unsupported pace profile version")
        rng = random.Random(seed)
        early = rng.uniform(.09, .13)
        middle = rng.uniform(.08, .11)
        finish = rng.uniform(.14, .18)
        corner = rng.uniform(.09, .12)
        if profile_version == 1:
            waves = [(rng.uniform(.02, .035), rng.uniform(85, 145), rng.uniform(0, 2*math.pi)),
                     (rng.uniform(.01, .02), rng.uniform(170, 260), rng.uniform(0, 2*math.pi))]
        else:
            waves = [(rng.uniform(.132, .14), rng.uniform(115, 140), rng.uniform(0, 2*math.pi)),
                     (rng.uniform(.04, .06), rng.uniform(180, 260), rng.uniform(0, 2*math.pi))]

        def transition(start, end, value):
            ratio = min(1.0, max(0.0, (value-start)/(end-start)))
            return ratio*ratio*(3-2*ratio)

        finish_length = min(150.0, distance*.25)
        # Effort uses segment distance; bends and seeded waves use absolute
        # distance. Two spatial smoothing passes anticipate and leave corners
        # gradually, including when a continuation starts in the middle of one.
        smoothing_step, smoothing_length, padding = .5, 5.0, 50.0
        bend_grid = [min(1.0, abs(path.at(offset-padding+i*smoothing_step)[2])*36.8)
                     for i in range(math.ceil((distance+2*padding)/smoothing_step)+1)]
        weight = 1-math.exp(-smoothing_step/smoothing_length)
        for i in range(1, len(bend_grid)):
            bend_grid[i] = bend_grid[i-1]+weight*(bend_grid[i]-bend_grid[i-1])
        for i in range(len(bend_grid)-2, -1, -1):
            bend_grid[i] = bend_grid[i+1]+weight*(bend_grid[i]-bend_grid[i+1])
        shape = []
        for arc in self.distances:
            progress = arc/distance
            effort = early-(early+middle)*transition(.18, .43, progress)
            effort += (middle+finish)*transition(distance-finish_length, distance-finish_length*.38, arc)
            index = (arc+padding)/smoothing_step
            lower = min(len(bend_grid)-2, int(index))
            bend = bend_grid[lower]+(index-lower)*(bend_grid[lower+1]-bend_grid[lower])
            fluctuation = sum(amplitude*math.sin(2*math.pi*(offset+arc)/wavelength+angle)
                              for amplitude, wavelength, angle in waves)
            shape.append((1+effort+fluctuation)*(1-corner*bend))
        peak_shape = max(shape)
        shape = [value/peak_shape for value in shape]
        caps = [min(6.0, math.sqrt(3.0/max(curvature, 1e-5))) for curvature in envelope]

        def constrained(strength):
            speeds = [cap*(1-strength*(1-factor)) for cap, factor in zip(caps, shape)]
            speeds[0] = speeds[-1] = 0.0
            for i in range(1, len(speeds)):
                speeds[i] = min(speeds[i], math.sqrt(speeds[i-1]**2+2*1.2*(self.distances[i]-self.distances[i-1])))
            for i in range(len(speeds)-2, -1, -1):
                speeds[i] = min(speeds[i], math.sqrt(speeds[i+1]**2+2*1.2*(self.distances[i+1]-self.distances[i])))
            duration = sum(2*(b-a)/(x+y) for a, b, x, y in zip(self.distances, self.distances[1:], speeds, speeds[1:]))
            return speeds, duration

        fastest_speeds, fastest = constrained(0.0)
        if fastest > self.duration:
            if not allow_slower:
                raise MotionError(f"Route requires at least {fastest:.1f}s under the motion limits; requested {self.duration:.1f}s")
            self.duration = fastest
        if self.duration > 300:
            raise MotionError("Motion-limited duration exceeds 300 seconds")
        speeds, shaped_duration = constrained(1.0)
        strength = 1.0
        if shaped_duration > self.duration:
            low, high = 0.0, 1.0
            speeds, shaped_duration = fastest_speeds, fastest
            # Attenuate the preferred profile only as much as feasibility requires.
            for _ in range(24):
                trial = (low+high)/2
                candidate, duration = constrained(trial)
                if duration <= self.duration:
                    low, speeds, shaped_duration = trial, candidate, duration
                else:
                    high = trial
            strength = low
        scale = min(1.0, shaped_duration/self.duration)
        self.speeds = [speed*scale for speed in speeds]
        self.profile = {"seed": seed, "profile_strength": strength,
                        "attenuated": strength < .999999,
                        "phases": {"early_end_m": .18*distance, "settle_end_m": .43*distance,
                                   "finish_start_m": distance-finish_length,
                                   "finish_peak_m": distance-finish_length*.38},
                        "variation": {"early_effort": early, "middle_easing": middle,
                                      "finish_effort": finish, "corner_reduction": corner,
                                      "corner_smoothing_length_m": smoothing_length, "waves": waves},
                        "minimum_duration_s": fastest}
        if profile_version >= 2:
            self.profile["version"] = profile_version
        self.times = [0.0]
        for a, b, x, y in zip(self.distances, self.distances[1:], self.speeds, self.speeds[1:]):
            self.times.append(self.times[-1]+2*(b-a)/(x+y))
        self.times[-1] = self.duration

    def at(self, time_s):
        if time_s >= self.duration:
            return self.distances[-1], 0.0
        upper = min(len(self.times)-1, max(1, bisect.bisect_right(self.times, max(0, time_s))))
        lower = upper-1
        dt = time_s-self.times[lower]
        acceleration = (self.speeds[upper]-self.speeds[lower])/(self.times[upper]-self.times[lower])
        return (self.distances[lower]+self.speeds[lower]*dt+.5*acceleration*dt*dt,
                self.speeds[lower]+acceleration*dt)


def build_path(data):
    if "track" in data:
        from stadium_track import StadiumPath
        return StadiumPath(data)
    return SmoothPath(data)


def filter_gait_acceleration(samples):
    """Limit gait bandwidth before emulator delivery, without gain or time shift."""
    half = GAIT_FILTER_TAPS//2
    normalized_cutoff = GAIT_FILTER_CUTOFF_HZ/HZ
    weights = []
    for index in range(GAIT_FILTER_TAPS):
        offset = index-half
        sinc = (2*normalized_cutoff if offset == 0 else
                math.sin(2*math.pi*normalized_cutoff*offset)/(math.pi*offset))
        weights.append(sinc*(.54-.46*math.cos(2*math.pi*index/(GAIT_FILTER_TAPS-1))))
    total = sum(weights)
    weights = [weight/total for weight in weights]
    original = [sample["gait_acceleration_world"] for sample in samples]
    for index, sample in enumerate(samples):
        if sample["speed_m_s"] == 0:
            sample["gait_acceleration_world"] = (0.0, 0.0, 0.0)
            continue
        sample["gait_acceleration_world"] = tuple(
            sum(weight*original[min(len(original)-1, max(0, index+tap-half))][axis]
                for tap, weight in enumerate(weights)) for axis in range(3))


def build_samples(data, reference, distance=1050, pace=250, cadence=175,
                  offset=0.0, phase=0.0, gravity=DEFAULT_GRAVITY, allow_slower=False, seed=0,
                  elapsed_offset_s=0.0, model_version=MOTION_MODEL_VERSION, elevation=None,
                  magnetic_field=DEFAULT_MAGNETIC_FIELD, magnetic_field_world=None):
    if not math.isfinite(cadence) or not 120 <= cadence <= 210:
        raise MotionError("Reference motion supports a requested cadence of 120 to 210")
    if not 8 <= norm(gravity) <= 11:
        raise MotionError("Unexpected emulator gravity baseline")
    if model_version not in (1, 2, 3, MOTION_MODEL_VERSION):
        raise MotionError("Unsupported motion model version")
    if not math.isfinite(elapsed_offset_s) or elapsed_offset_s < 0:
        raise MotionError("Invalid accumulated motion time")
    if len(magnetic_field) != 3 or not all(math.isfinite(value) for value in magnetic_field) or not 1 <= norm(magnetic_field) <= 2000:
        raise MotionError("Invalid magnetic-field baseline")
    path = build_path(data)
    law = TimeLaw(path, distance, pace, offset, allow_slower=allow_slower, seed=seed,
                  profile_version=2 if model_version >= 4 else 1)
    times = [i/HZ for i in range(math.ceil(law.duration*HZ))]+[law.duration]
    mount = align_vectors(gravity, (0, 0, 1))
    initial_heading = path.at(0)[1]
    samples = []
    previous_rate = 0.0
    phase_start = phase
    reference_cadence = (getattr(reference, "cadence_legacy", reference.cadence)
                         if model_version == 1 else reference.cadence)
    cadence_rng = random.Random(f"cadence-v2:{seed}")
    cadence_phases = (cadence_rng.uniform(0, 2*math.pi), cadence_rng.uniform(0, 2*math.pi))
    height = None
    if model_version >= 2:
        from elevation_profile import ElevationProfile
        if elevation is None:
            height = ElevationProfile(seed=seed)
        else:
            height = ElevationProfile.from_metadata(elevation)
    for i, time_s in enumerate(times):
        arc, speed = law.at(time_s)
        point, heading, curvature = path.at(offset+arc)
        envelope = min(1.0, max(0.0, speed/2.0))
        envelope = envelope*envelope*(3-2*envelope)
        absolute_time = elapsed_offset_s+time_s
        if model_version == 1:
            cadence_now = cadence*envelope*max(.78, min(1.20, (speed/(1000/pace))**.32))
        else:
            # Cadence and stride shorten together while slowing down. Multiplying
            # cadence by the amplitude envelope made stride diverge near rest.
            wander = (1+.025*math.sin(2*math.pi*absolute_time/17+cadence_phases[0])
                      +.015*math.sin(2*math.pi*absolute_time/43+cadence_phases[1]))
            cadence_now = cadence*(speed/(1000/pace))**.32*wander
        phase_rate = cadence_now/reference_cadence
        if i:
            phase += .5*(phase_rate+previous_rate)*(time_s-times[i-1])
        previous_rate = phase_rate
        if model_version >= 2 and hasattr(reference, "sample_aligned"):
            residual_pose, local_acceleration = reference.sample_aligned(phase, gravity)
        else:
            residual_pose, local_acceleration = reference.sample(phase)
        course = axis_angle((0, 0, 1), heading-initial_heading)
        body = qmul(course, mount)
        pose = qmul(body, slerp((1, 0, 0, 0), residual_pose, envelope))
        # Preserve the measured force envelope when changing cadence. Scaling
        # by frequency squared assumes a fixed phone displacement and can drive
        # a valid cadence beyond the tablet's accelerometer range.
        impact_scale = (envelope*phase_rate**2 if model_version == 1 else
                        envelope*max(.75, min(1.25, (speed/(1000/pace))**.35)))
        gait = rotate(body, tuple(x*impact_scale for x in local_acceleration))
        samples.append({"t_s": time_s, "distance_m": arc, "arc_m": offset+arc,
                        "position_m": point, "longitude": path.coordinates(point)[0],
                        "latitude": path.coordinates(point)[1], "speed_m_s": speed,
                        "heading_rad": heading, "curvature": curvature, "pose": pose,
                        "cadence_nominal_spm": cadence_now,
                        "step_length_m": speed*60/cadence_now if cadence_now > 0 else 0.0,
                        "step_count_model": phase*reference_cadence/60,
                        "absolute_time_s": absolute_time,
                        "gait_acceleration_world": gait, "phase_s": phase,
                        "velocity_world": (speed*math.cos(heading), speed*math.sin(heading), 0.0)})
        samples[-1].update(height.at(absolute_time, offset+arc) if height is not None else
                           {"ground_altitude_m": 50.0, "altitude_m": 50.0})
        if model_version >= 2 and samples[-1]["step_length_m"] > MAX_MODEL_STEP_LENGTH_M:
            raise MotionError("当前配速与步频会产生超过 2.1 米的模型单步距离，请提高步频或降低速度")
    if model_version >= 3:
        filter_gait_acceleration(samples)
    magnetic_world = magnetic_field_world if magnetic_field_world is not None else rotate(samples[0]["pose"], magnetic_field)
    if len(magnetic_world) != 3 or not all(math.isfinite(value) for value in magnetic_world) or not 1 <= norm(magnetic_world) <= 2000:
        raise MotionError("Invalid fixed world magnetic field")
    for i, sample in enumerate(samples):
        left = samples[max(0, i-1)]
        right = samples[min(len(samples)-1, i+1)]
        dt = right["t_s"]-left["t_s"]
        route_acceleration = tuple((b-a)/dt for a, b in zip(left["velocity_world"], right["velocity_world"]))
        if i in (0, len(samples)-1):
            route_acceleration = (0.0, 0.0, 0.0)
        sample["route_acceleration_world"] = route_acceleration
        force_world = tuple(route_acceleration[j]+sample["gait_acceleration_world"][j]+(norm(gravity) if j == 2 else 0) for j in range(3))
        sample["acceleration"] = rotate(conjugate(sample["pose"]), force_world)
        if model_version >= 2:
            from sensor_channels import android_orientation
            sample["gravity_body"] = rotate(conjugate(sample["pose"]), (0, 0, norm(gravity)))
            sample["linear_acceleration"] = tuple(a-g for a, g in zip(sample["acceleration"], sample["gravity_body"]))
            sample["magnetic_field"] = rotate(conjugate(sample["pose"]), magnetic_world)
            sample["orientation_deg"] = android_orientation(sample["gravity_body"], sample["magnetic_field"])
        # Store the body angular rate for this sample's outgoing interval.
        if i+1 < len(samples):
            sample["gyroscope"] = angular_velocity(sample["pose"], samples[i+1]["pose"], samples[i+1]["t_s"]-sample["t_s"])
        else:
            sample["gyroscope"] = (0.0, 0.0, 0.0)
    metadata = {"duration_s": law.duration, "distance_m": distance, "pace_target_s_km": pace,
                     "planned_pace_s_km": law.duration*1000/distance,
                     "cadence_target": cadence, "offset_m": offset, "loop_length_m": path.length,
                     "motion_model_version": model_version, "elapsed_offset_s": elapsed_offset_s,
                     "phase_start_s": phase_start, "reference_step_cadence_spm": reference_cadence,
                     "step_length_model_limit_m": MAX_MODEL_STEP_LENGTH_M if model_version >= 2 else None,
                     "expected_steps": samples[-1]["step_count_model"]-samples[0]["step_count_model"],
                     "magnetic_field_baseline": magnetic_field, "magnetic_field_world": magnetic_world,
                     "elevation_profile": height.metadata if height is not None else None,
                     "gait_frame_alignment": "per_sample_source_up" if model_version >= 2 and hasattr(reference, "sample_aligned") else "legacy_or_test_fixture",
                     "impact_model": ("legacy_frequency_squared" if model_version == 1 else
                                      "measured_force_with_speed_envelope" if model_version == 2 else
                                      "measured_force_with_speed_envelope_and_gait_antialias"),
                     "pace_profile": law.profile,
                     "gravity_baseline": gravity, "reference_sha256": reference.sha256,
                     "reference_template_period_s": reference.period,
                "reference_loops_elapsed": (samples[-1]["phase_s"]-samples[0]["phase_s"])/reference.period}
    if model_version >= 3:
        metadata["gait_antialias_filter"] = {
            "kind": "Hamming-windowed sinc FIR", "cutoff_hz": GAIT_FILTER_CUTOFF_HZ,
            "taps": GAIT_FILTER_TAPS, "sample_rate_hz": HZ, "phase": "zero",
            "dc_gain": 1.0, "scope": "world gait acceleration only",
            "edge_extension": "nearest", "rest_endpoints": "exact zero"}
    from sensor_channels import COUPLED_SENSORS
    metadata["sensor_channels"] = list(COUPLED_SENSORS) if model_version >= 2 else ["acceleration", "gyroscope"]
    metadata["sensor_axis_limits"] = {"acceleration_m_s2": 39.3, "gyroscope_rad_s": 16.46, "magnetic_field_ut": 2000}
    if "track" in data:
        metadata["track"] = data["track"]
        metadata["checkpoint_source"] = data.get("checkpoint_source", "来源未核实")
        metadata["laps"] = distance/path.length
        metadata["pitch_xy"] = [path.project(point) for point in data.get("pitch_polygon_wgs84", [])]
        metadata["track_outline_xy"] = [path.at(i*path.length/160)[0] for i in range(161)]
        metadata["checkpoints"] = [{"name": name, "position_xy": path.project(point),
                                    "distance_to_track_m": path.distance_to(point),
                                    "app_hit_radius_m": None} for name, point in data.get("checkpoint_markers", {}).items()]
    return samples, metadata


def verify_samples(samples, metadata):
    if len(samples) < 3:
        raise MotionError("Too few generated samples")
    gravity = norm(metadata["gravity_baseline"])
    force_errors, velocity_errors, gyro_errors, gaps, speed_errors = [], [], [], [], []
    step_errors, cadence_errors = [], []
    magnetic_errors, gravity_errors = [], []
    for i, sample in enumerate(samples):
        scalars = [value for value in sample.values() if isinstance(value, (float, int))]
        scalars += [v for value in sample.values() if isinstance(value, (list, tuple)) for v in value]
        if not all(math.isfinite(v) for v in scalars):
            raise MotionError("Generated non-finite data")
        if abs(norm(sample["pose"])-1) > 1e-6 or norm(sample["acceleration"]) > 150 or norm(sample["gyroscope"]) > 40:
            raise MotionError("Generated pose or sensor magnitude exceeds engineering bounds")
        reconstructed = rotate(sample["pose"], sample["acceleration"])
        expected = tuple(sample["route_acceleration_world"][j]+sample["gait_acceleration_world"][j]+(gravity if j == 2 else 0) for j in range(3))
        force_errors.append(norm(tuple(a-b for a, b in zip(reconstructed, expected))))
        if metadata.get("motion_model_version", 1) >= 2:
            from sensor_channels import android_orientation
            if any(abs(value) > 39.3 for value in sample["acceleration"]) or any(abs(value) > 16.46 for value in sample["gyroscope"]):
                raise MotionError("当前动作参考超出设备加速度或角速度范围，请降低目标步频或速度")
            step_errors.append(abs(sample["step_length_m"]*sample["cadence_nominal_spm"]/60-sample["speed_m_s"]))
            if sample["step_length_m"] < 0 or sample["step_length_m"] > MAX_MODEL_STEP_LENGTH_M:
                raise MotionError("Generated step length exceeds the configured model range")
            expected_magnetic = rotate(sample["pose"], sample["magnetic_field"])
            magnetic_errors.append(norm(tuple(a-b for a, b in zip(expected_magnetic, metadata["magnetic_field_world"]))))
            expected_gravity = rotate(sample["pose"], sample["gravity_body"])
            gravity_errors.append(norm(tuple(a-b for a, b in zip(expected_gravity, (0, 0, gravity)))))
            orientation = android_orientation(sample["gravity_body"], sample["magnetic_field"])
            if any(abs((value-expected+180) % 360-180) > 1e-6 for value, expected in zip(sample["orientation_deg"], orientation)):
                raise MotionError("Orientation disagrees with gravity and magnetic field")
            if norm(tuple(a-g-l for a, g, l in zip(sample["acceleration"], sample["gravity_body"], sample["linear_acceleration"]))) > 1e-6:
                raise MotionError("Gravity and linear acceleration disagree with raw acceleration")
        if i:
            previous = samples[i-1]
            dt = sample["t_s"]-previous["t_s"]
            if dt <= 0 or sample["distance_m"] < previous["distance_m"]:
                raise MotionError("Time or route distance went backwards")
            gaps.append(dt)
            displacement = norm(tuple(b-a for a, b in zip(previous["position_m"], sample["position_m"])))
            expected_speed = .5*(sample["speed_m_s"]+previous["speed_m_s"])
            velocity_errors.append(abs(displacement/dt-expected_speed))
            speed_errors.append(abs(sample["speed_m_s"]-previous["speed_m_s"])/dt)
            derived = angular_velocity(previous["pose"], sample["pose"], dt)
            gyro_errors.append(norm(tuple(a-b for a, b in zip(derived, previous["gyroscope"]))))
            if metadata.get("motion_model_version", 1) >= 2:
                phase_cadence = (sample["phase_s"]-previous["phase_s"])/dt*metadata["reference_step_cadence_spm"]
                cadence_errors.append(abs(phase_cadence-.5*(sample["cadence_nominal_spm"]+previous["cadence_nominal_spm"])))
    checks = {"force_reconstruction_max_m_s2": max(force_errors),
              "pose_gyro_max_rad_s": max(gyro_errors),
              "position_speed_max_m_s": max(velocity_errors),
              "max_sample_gap_s": max(gaps),
              "longitudinal_acceleration_max_m_s2": max(speed_errors),
              "sensor_acceleration_peak_m_s2": max(norm(s["acceleration"]) for s in samples),
              "sensor_gyro_peak_rad_s": max(norm(s["gyroscope"]) for s in samples),
              "speed_peak_m_s": max(s["speed_m_s"] for s in samples),
              "turn_lateral_acceleration_peak_m_s2": max(abs(s["curvature"])*s["speed_m_s"]**2 for s in samples)}
    if step_errors:
        checks.update(step_speed_identity_max_m_s=max(step_errors), cadence_phase_max_error_spm=max(cadence_errors),
                      magnetic_world_max_error_ut=max(magnetic_errors), gravity_world_max_error_m_s2=max(gravity_errors),
                      step_length_peak_m=max(s["step_length_m"] for s in samples),
                      altitude_min_m=min(s["altitude_m"] for s in samples), altitude_max_m=max(s["altitude_m"] for s in samples))
    passed = (checks["force_reconstruction_max_m_s2"] < 1e-6 and
              checks["pose_gyro_max_rad_s"] < 1e-6 and
              checks["position_speed_max_m_s"] < .15 and
              checks["max_sample_gap_s"] < .050001 and
              checks["longitudinal_acceleration_max_m_s2"] < 1.21 and
              checks["speed_peak_m_s"] <= 6.0001 and
              checks["turn_lateral_acceleration_peak_m_s2"] <= 3.3 and
              abs(samples[-1]["distance_m"]-metadata["distance_m"]) < 1e-6)
    if step_errors:
        passed = (passed and max(step_errors) < 1e-9 and max(cadence_errors) < 1e-7
                  and max(magnetic_errors) < 1e-6 and max(gravity_errors) < 1e-6)
    checks["passed"] = passed
    if not passed:
        raise MotionError("Offline motion consistency failed: "+json.dumps(checks))
    if metadata.get("motion_model_version", 1) >= 2 and metadata.get("gait_frame_alignment") == "per_sample_source_up":
        from motion_reference import estimate_period
        windows = []
        width = 10*HZ
        for first in range(0, len(samples)-width+1, width):
            group = samples[first:first+width]
            cadences = [sample["cadence_nominal_spm"] for sample in group]
            if min(sample["speed_m_s"] for sample in group) < 2 or max(cadences) > 1.35*min(cadences):
                continue
            measured = estimate_period([norm(sample["acceleration"]) for sample in group], HZ)
            requested = statistics.fmean(cadences)
            relative_error = abs(measured["cadence_estimate"]/requested-1)
            windows.append({"start_s": group[0]["t_s"], "model_cadence_spm": requested,
                            "waveform_cadence_spm": measured["cadence_estimate"], "relative_error": relative_error})
            if relative_error > .12:
                raise MotionError("动作波形与模型步频偏差超过 12%，请重新生成并检查参考动作")
        checks["waveform_cadence_windows"] = windows
        checks["waveform_cadence_max_relative_error"] = max((window["relative_error"] for window in windows), default=None)
    return checks


def signal_metrics(accelerations, gyroscopes, hz):
    from motion_reference import estimate_period
    magnitudes = [norm(a) for a in accelerations]
    gyro_magnitudes = [norm(g) for g in gyroscopes]
    mean = statistics.fmean(magnitudes)
    signal = [value-mean for value in magnitudes]
    # A sampled spectrum, explicitly reported as a descriptor, not a classifier.
    frequencies = [i/10 for i in range(5, 81)]
    powers = []
    for frequency in frequencies:
        real = sum(value*math.cos(2*math.pi*frequency*i/hz) for i, value in enumerate(signal))
        imaginary = sum(value*math.sin(2*math.pi*frequency*i/hz) for i, value in enumerate(signal))
        powers.append((real*real+imaginary*imaginary)/len(signal)**2)
    total = sum(powers)
    def correlation(a, b):
        am, bm = statistics.fmean(a), statistics.fmean(b)
        denominator = math.sqrt(sum((x-am)**2 for x in a)*sum((y-bm)**2 for y in b))
        return sum((x-am)*(y-bm) for x, y in zip(a, b))/denominator if denominator > 1e-12 else None
    ordered = sorted(magnitudes)
    return {"step_period_estimate": estimate_period(magnitudes, hz),
            "acceleration_norm_mean": mean,
            "acceleration_norm_std": statistics.pstdev(magnitudes),
            "acceleration_norm_p95": ordered[int(.95*(len(ordered)-1))],
            "gyro_norm_rms": math.sqrt(statistics.fmean(v*v for v in gyro_magnitudes)),
            "spectrum_peak_hz": frequencies[powers.index(max(powers))],
            "spectrum_high_band_fraction": sum(power for frequency, power in zip(frequencies, powers) if frequency >= 4)/max(total, 1e-12),
            "acceleration_gyro_norm_correlation": correlation(magnitudes, gyro_magnitudes),
            "acceleration_axis_correlations": [correlation([a[i] for a in accelerations], [a[j] for a in accelerations]) for i, j in ((0, 1), (0, 2), (1, 2))]}
