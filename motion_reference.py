"""Pinned public MotionSense reference, coordinate conversion and signal utilities.

Core Motion gravity points down; Android specific force at rest points up.
The dynamic conversion uses -9.80665 * (gravity + userAcceleration). The
dataset has no measured running speed and is not a personal gait calibration.
"""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
from urllib.request import Request, urlopen
import zipfile

from emulator_motion import MotionError

G = 9.80665
ROOT = Path(__file__).resolve().parent
REFERENCE_DIR = ROOT / ".lab/reference/motionsense"
COMMIT = "261cb0d60611a049ab38127738e42cc510ed50f4"
ARCHIVE_SHA256 = "bc632abddbb1e92db31daa7b740710bc8f756f90febe385e6f1dc32ff8d92285"
LICENSE_SHA256 = "48035d625af1c7646615541a37438e20127e52214ec832a19aec470d016733a4"
BASE_URL = f"https://raw.githubusercontent.com/mmalekzadeh/motion-sense/{COMMIT}/"
TRAIN_MEMBER = "A_DeviceMotion_data/jog_9/sub_1.csv"
HELDOUT_MEMBER = "A_DeviceMotion_data/jog_9/sub_24.csv"


def norm(vector):
    return math.sqrt(sum(value * value for value in vector))


def unit(vector):
    length = norm(vector)
    if not math.isfinite(length) or length < 1e-12:
        raise MotionError("Zero or invalid vector/quaternion")
    return tuple(value / length for value in vector)


def qmul(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v)


def conjugate(q):
    return (q[0], -q[1], -q[2], -q[3])


def rotate(q, vector):
    return qmul(qmul(q, (0.0, *vector)), conjugate(q))[1:]


def axis_angle(axis, angle):
    direction = unit(axis)
    return (math.cos(angle / 2), *(v * math.sin(angle / 2) for v in direction))


def slerp(a, b, fraction):
    dot = sum(x*y for x, y in zip(a, b))
    if dot < 0:
        b, dot = tuple(-x for x in b), -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        return unit(tuple(x + fraction*(y-x) for x, y in zip(a, b)))
    angle = math.acos(dot)
    left = math.sin((1-fraction)*angle) / math.sin(angle)
    right = math.sin(fraction*angle) / math.sin(angle)
    return tuple(left*x + right*y for x, y in zip(a, b))


def align_vectors(source, target):
    a, b = unit(source), unit(target)
    dot = sum(x*y for x, y in zip(a, b))
    cross = (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    if dot < -0.999999:
        axis = (0, -a[2], a[1]) if abs(a[0]) < .9 else (-a[1], a[0], 0)
        return axis_angle(axis, math.pi)
    return unit((1+dot, *cross))


def angular_velocity(left, right, dt):
    if dt <= 0:
        raise MotionError("Angular velocity requires positive elapsed time")
    delta = unit(qmul(conjugate(left), right))
    if delta[0] < 0:
        delta = tuple(-v for v in delta)
    sine = norm(delta[1:])
    if sine < 1e-12:
        return (0.0, 0.0, 0.0)
    angle = 2 * math.atan2(sine, max(0.0, delta[0]))
    return tuple(v * angle / (sine * dt) for v in delta[1:])


def attitude_quaternion(roll, pitch, yaw):
    # Core Motion Z-X-Y attitude reproduces the published gravity channels.
    return qmul(qmul(axis_angle((0, 0, 1), yaw), axis_angle((1, 0, 0), pitch)),
                axis_angle((0, 1, 0), roll))


def unwrap(values):
    result = [values[0]]
    for value in values[1:]:
        delta = (value-result[-1]+math.pi) % (2*math.pi) - math.pi
        result.append(result[-1] + delta)
    return result


def moving_mean(values, radius):
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1]+value)
    return [(prefix[min(len(values), i+radius+1)]-prefix[max(0, i-radius)]) /
            (min(len(values), i+radius+1)-max(0, i-radius)) for i in range(len(values))]


def download_reference():
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, remote, expected in (
        ("A_DeviceMotion_data.zip", "data/A_DeviceMotion_data.zip", ARCHIVE_SHA256),
        ("LICENSE", "LICENSE", LICENSE_SHA256),
    ):
        destination = REFERENCE_DIR / name
        if not destination.exists():
            partial = destination.with_suffix(destination.suffix + ".partial")
            try:
                with urlopen(Request(BASE_URL+remote, headers={"User-Agent": "MotionReference/1"}), timeout=45) as response, partial.open("wb") as stream:
                    size = 0
                    while block := response.read(1024*1024):
                        size += len(block)
                        if size > 100*1024*1024:
                            raise MotionError("Reference download exceeds 100 MiB")
                        stream.write(block)
                if hashlib.sha256(partial.read_bytes()).hexdigest() != expected:
                    raise MotionError("Reference download checksum mismatch")
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise MotionError("Cached reference checksum mismatch")


def read_record(member):
    archive = REFERENCE_DIR / "A_DeviceMotion_data.zip"
    if not archive.exists() or archive.stat().st_size > 100*1024*1024 or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise MotionError("Run --prepare-reference to obtain the pinned reference")
    with zipfile.ZipFile(archive) as source:
        if source.getinfo(member).file_size > 20*1024*1024:
            raise MotionError("Reference CSV is unexpectedly large")
        rows = list(csv.DictReader(io.StringIO(source.read(member).decode("utf-8"))))
    records = []
    for row in rows[250:-250]:  # Remove five seconds of handling at each end.
        record = {key: float(value) for key, value in row.items() if key}
        if not all(math.isfinite(value) for value in record.values()):
            raise MotionError("Non-finite public reference sample")
        records.append(record)
    if len(records) < 500:
        raise MotionError("Insufficient jogging reference samples")
    return records


def channel(row, name):
    return tuple(row[name+"."+axis] for axis in "xyz")


CADENCE_METHOD = "stride-autocorrelation-step-spectrum-v2"


def estimate_period(signal, hz):
    """Describe cadence in the supported 120--210 steps/minute jogging band.

    A stride contains two alternating foot contacts. Pocket signals can repeat
    most strongly at that two-step period, while a norm can emphasize higher
    harmonics. Select a stride autocorrelation peak only when its half-period
    has spectral support in the step-frequency band. This is a signal estimate,
    without foot-contact labels or an App step-detector calibration.
    """
    if not math.isfinite(hz) or hz < 10 or not all(math.isfinite(v) for v in signal):
        raise MotionError("Invalid cadence signal or sample rate")
    window_size = round(10*hz)
    windows = []
    for start in range(0, len(signal), window_size):
        values = signal[start:start+window_size]
        if len(values) < 5*hz:
            continue
        mean = statistics.fmean(values)
        centered = [v-mean for v in values]
        if sum(v*v for v in centered) < 1e-12:
            continue
        tapered = [v*(.5-.5*math.cos(2*math.pi*i/(len(values)-1)))
                   for i, v in enumerate(centered)]

        def power(frequency):
            real = sum(v*math.cos(2*math.pi*frequency*i/hz) for i, v in enumerate(tapered))
            imaginary = sum(v*math.sin(2*math.pi*frequency*i/hz) for i, v in enumerate(tapered))
            return real*real+imaginary*imaginary

        band_peak = max(power(2+i*.025) for i in range(61))
        if band_peak < .005*len(tapered)*sum(v*v for v in tapered):
            continue
        first, last = math.floor(2*hz/3.5), math.ceil(2*hz/2)
        correlations = {}
        for lag in range(first-1, last+2):
            left, right = centered[:-lag], centered[lag:]
            scale = math.sqrt(sum(v*v for v in left)*sum(v*v for v in right))
            correlations[lag] = sum(a*b for a, b in zip(left, right))/max(scale, 1e-12)
        candidates = []
        for lag in range(first, last+1):
            left, score, right = (correlations[lag-1], correlations[lag], correlations[lag+1])
            if score < left or score < right or score < .35:
                continue
            curvature = left-2*score+right
            fraction = .5*(left-right)/curvature if abs(curvature) > 1e-12 else 0.0
            stride_period = (lag+max(-.5, min(.5, fraction)))/hz
            cadence = 120/stride_period
            support = power(2/stride_period)/max(band_peak, 1e-12)
            if 120 <= cadence <= 210 and support >= .15:
                candidates.append((score, stride_period, support))
        if candidates:
            score, stride_period, support = max(candidates)
            windows.append({"start_s": start/hz, "stride_period_s": stride_period,
                            "cadence_estimate": 120/stride_period,
                            "autocorrelation": score, "step_spectral_support": support})
    expected_windows = sum(len(signal[start:start+window_size]) >= 5*hz
                           for start in range(0, len(signal), window_size))
    cadence = statistics.median(w["cadence_estimate"] for w in windows) if windows else None
    deviation = max(abs(w["cadence_estimate"]-cadence)/cadence for w in windows) if windows else None
    confident = len(windows) >= 3 and len(windows) == expected_windows and deviation <= .08
    return {"method": CADENCE_METHOD, "period_s": 60/cadence if cadence else None,
            "step_period_s": 60/cadence if cadence else None,
            "stride_period_s": 120/cadence if cadence else None,
            "cadence_estimate": cadence,
            "autocorrelation": statistics.median(w["autocorrelation"] for w in windows) if windows else 0.0,
            "autocorrelation_period": "stride_period_s", "steps_per_stride": 2,
            "cadence_unit": "steps/minute", "confidence": "high" if confident else "low",
            "supported_cadence_range_spm": [120, 210],
            "window_count": expected_windows, "accepted_window_count": len(windows),
            "max_window_relative_deviation": deviation, "windows": windows,
            "selection_basis": "Two-step recurrence with step-band spectral support; 10-second window agreement",
            "limitation": "Signal periodicity estimate; foot contacts and App-reported cadence remain unvalidated"}


def source_cadence(rows):
    # Include gravity before taking the norm. Norm(userAcceleration) alone
    # rectifies the dynamic signal and can select an unrelated higher harmonic.
    magnitudes = [norm(tuple(g+a for g, a in zip(channel(row, "gravity"),
                                                channel(row, "userAcceleration")))) for row in rows]
    result = estimate_period(magnitudes, 50)
    if result["confidence"] != "high":
        raise MotionError("Public reference cadence is ambiguous; source calibration failed")
    return result


def source_frame(poses):
    """Reproduce the template's slow phone frame and describe its varying up axis."""
    continuous = []
    for pose in poses:
        if continuous and sum(a*b for a, b in zip(continuous[-1], pose)) < 0:
            pose = tuple(-v for v in pose)
        continuous.append(pose)
    slow_components = [moving_mean([q[j] for q in continuous], 50) for j in range(4)]
    bases = [unit(tuple(column[i] for column in slow_components)) for i in range(len(continuous))]
    up_axes = [rotate(conjugate(base), (0, 0, 1)) for base in bases]
    mean_up = unit(tuple(statistics.fmean(axis[j] for axis in up_axes) for j in range(3)))
    angles = sorted(math.degrees(math.acos(max(-1.0, min(1.0, sum(a*b for a, b in zip(mean_up, axis))))))
                    for axis in up_axes)
    metadata = {
        "source_up_axis": mean_up,
        "source_up_axis_source": "checksum-verified source attitude; inverse slow quaternion rotates world up into template frame",
        "source_up_axis_smoothing_radius_samples": 50,
        "source_up_axis_deviation_deg": {
            "min": angles[0], "median": statistics.median(angles),
            "p95": angles[int(.95*(len(angles)-1))], "max": angles[-1],
            "rms": math.sqrt(statistics.fmean(angle*angle for angle in angles)),
            "sample_count": len(angles), "source_hz": 50,
        },
        "source_up_axis_limitation": "Global mean only; low-frequency orientation varies and mean alignment cannot restore each source frame",
    }
    return bases, metadata


def source_up_samples(bases):
    """Follow the template's five-tap filter and 50 -> 20 Hz sample times."""
    up_axes = [rotate(conjugate(base), (0, 0, 1)) for base in bases]
    weights = (1, 4, 6, 4, 1)
    filtered = [tuple(sum(weights[k]*up_axes[i+k-2][j] for k in range(5))/16
                      for j in range(3)) for i in range(2, len(up_axes)-2)]
    result = []
    for tick in range(int((len(filtered)-1)*20/50)):
        index = tick*50/20
        lower = int(index)
        fraction = index-lower
        result.append(unit(tuple(a+(b-a)*fraction
                                 for a, b in zip(filtered[lower], filtered[lower+1]))))
    return result


def prepare_template():
    download_reference()
    rows = read_record(TRAIN_MEMBER)
    poses, accelerations, gravity_errors = [], [], []
    for row in rows:
        raw = attitude_quaternion(row["attitude.roll"], row["attitude.pitch"], row["attitude.yaw"])
        expected_gravity = rotate(conjugate(raw), (0, 0, -1))
        gravity_errors.append(norm(tuple(a-b for a, b in zip(expected_gravity, channel(row, "gravity")))))
        pose = raw
        if poses and sum(a*b for a, b in zip(poses[-1], pose)) < 0:
            pose = tuple(-x for x in pose)
        poses.append(pose)
        accelerations.append(rotate(pose, tuple(-G*x for x in channel(row, "userAcceleration"))))
    if max(gravity_errors) > .02:
        raise MotionError("Reference attitude/gravity convention check failed")
    measured_rates = [channel(row, "rotationRate") for row in rows]
    source_rate_errors = []
    for i in range(len(poses)-1):
        derived = angular_velocity(poses[i], poses[i+1], .02)
        measured = tuple((a+b)/2 for a, b in zip(measured_rates[i], measured_rates[i+1]))
        source_rate_errors.append(norm(tuple(a-b for a, b in zip(derived, measured))))
    # Remove the source's slow orientation with quaternions, not Euler yaw.
    # The latter becomes unstable when a pocket phone crosses a gimbal pole.
    bases, frame_metadata = source_frame(poses)
    poses = [qmul(conjugate(base), q) for base, q in zip(bases, poses)]
    accelerations = [rotate(conjugate(base), a) for base, a in zip(bases, accelerations)]
    # Remove slow path acceleration from the source. Route acceleration is added
    # separately, so the source's original corners are not replayed as our path.
    drift = [moving_mean([a[i] for a in accelerations], 50) for i in range(3)]
    accelerations = [tuple(a[j]-drift[j][i] for j in range(3)) for i, a in enumerate(accelerations)]
    period = source_cadence(rows)
    # Low-pass every synchronized channel before 50 -> 20 Hz resampling.
    weights = (1, 4, 6, 4, 1)
    filtered = []
    for i in range(2, len(rows)-2):
        pose = unit(tuple(sum(weights[k]*poses[i+k-2][j] for k in range(5))/16 for j in range(4)))
        acceleration = tuple(sum(weights[k]*accelerations[i+k-2][j] for k in range(5))/16 for j in range(3))
        filtered.append((pose, acceleration))
    samples = []
    for tick in range(int((len(filtered)-1)*20/50)):
        index = tick*50/20
        lower = int(index)
        fraction = index-lower
        left, right = filtered[lower], filtered[lower+1]
        samples.append({"q": slerp(left[0], right[0], fraction),
                        "a": tuple(x+(y-x)*fraction for x, y in zip(left[1], right[1]))})
    template = {
        "schema": 1, "source_commit": COMMIT, "archive_sha256": ARCHIVE_SHA256,
        "source_member": TRAIN_MEMBER, "heldout_member": HELDOUT_MEMBER,
        "source_hz": 50, "hz": 20, "samples": samples, "cadence": period,
        "gravity_max_error_g": max(gravity_errors), "overlap_s": 2.0,
        "source_pose_gyro_rmse_rad_s": math.sqrt(statistics.fmean(error*error for error in source_rate_errors)),
        "source_gyro_rms_rad_s": math.sqrt(statistics.fmean(norm(rate)**2 for rate in measured_rates)),
        "limitations": ["Public single-subject reference; personal similarity unknown",
                        "Ground speed was not measured; target-pace biomechanics unknown",
                        "Source timing uses the documented nominal 50 Hz; source gyro/pose error is reported",
                        "Slow source orientation is removed; a finite template repeats with a two-second crossfade",
                        "Dynamic iOS-to-Android sign convention has no physical-phone validation"],
    }
    template.update(frame_metadata)
    path = REFERENCE_DIR / "template.json"
    path.write_text(json.dumps(template, ensure_ascii=False, separators=(",", ":"))+"\n")
    manifest = {key: value for key, value in template.items() if key != "samples"}
    manifest.update({"url": BASE_URL, "license": "MIT; cached LICENSE",
                     "template_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "samples": len(samples)})
    (REFERENCE_DIR/"source.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    return path, manifest


class Reference:
    def __init__(self, path):
        path = Path(path)
        if path.stat().st_size > 8*1024*1024:
            raise MotionError("Reference template exceeds size bound")
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        data = json.loads(path.read_text())
        if (data.get("schema") != 1 or data.get("archive_sha256") != ARCHIVE_SHA256 or
                data.get("source_commit") != COMMIT or data.get("source_member") != TRAIN_MEMBER or data.get("hz") != 20):
            raise MotionError("Unsupported reference identity or sample rate")
        manifest = json.loads((REFERENCE_DIR / "source.json").read_text())
        if manifest.get("template_sha256") != self.sha256:
            raise MotionError("Reference template does not match its prepared manifest")
        self.samples = data["samples"]
        if not 200 <= len(self.samples) <= 6000:
            raise MotionError("Unexpected reference sample count")
        for sample in self.samples:
            if len(sample["q"]) != 4 or len(sample["a"]) != 3:
                raise MotionError("Invalid reference sample shape")
            if abs(norm(sample["q"])-1) > 1e-5 or norm(sample["a"]) > 150:
                raise MotionError("Invalid reference pose or acceleration")
            if not all(math.isfinite(v) for v in (*sample["q"], *sample["a"])):
                raise MotionError("Non-finite reference input")
        self.hz = 20
        self.duration = (len(self.samples)-1)/self.hz
        self.overlap = 2.0
        self.period = self.duration-self.overlap
        # Recalibrate from the checksum-verified source, including legacy caches
        # whose 200 spm label came from a narrow-band harmonic. Cache bytes and
        # their historical SHA remain unchanged; runtime metadata records both.
        rows = read_record(TRAIN_MEMBER)
        calibration = source_cadence(rows)
        source_poses = [attitude_quaternion(row["attitude.roll"], row["attitude.pitch"], row["attitude.yaw"])
                        for row in rows]
        bases, frame_metadata = source_frame(source_poses)
        self.source_up_axis = frame_metadata["source_up_axis"]
        self.source_up_axes = source_up_samples(bases)
        if len(self.source_up_axes) != len(self.samples):
            raise MotionError("Source orientation timeline does not match cached template")
        self._aligned_cache = {}
        self.cadence_legacy = float(data["cadence"]["cadence_estimate"])
        if not math.isfinite(self.cadence_legacy) or not 120 <= self.cadence_legacy <= 210:
            raise MotionError("Invalid cached reference cadence estimate")
        self.cadence = float(calibration["cadence_estimate"])
        if not math.isfinite(self.cadence) or not 120 <= self.cadence <= 210:
            raise MotionError("Invalid reference cadence estimate")
        self.metadata = {key: value for key, value in data.items() if key != "samples"}
        self.metadata["cached_cadence"] = data["cadence"]
        self.metadata["cadence"] = calibration
        self.metadata["cadence_calibration_source"] = "checksum-verified source specific-force norm"
        self.metadata["cadence_cache_recalibrated"] = data["cadence"] != calibration
        self.metadata["cadence_legacy"] = self.cadence_legacy
        self.metadata.update(frame_metadata)
        self.metadata["source_alignment"] = {
            "method": "per-source-sample-up-conjugation-v1",
            "timebase": "trimmed 50 Hz source index 2 + 2.5 * output_tick; original five-tap weights then unit interpolation",
            "targets": [],
            "limitation": "Up axes inherit source attitude and resampling uncertainty; aligned interpolation and crossfade do not establish physical foot contacts",
        }

    def interpolate(self, time_s, samples=None):
        samples = self.samples if samples is None else samples
        index = max(0.0, min(self.duration, time_s))*self.hz
        lower = min(len(self.samples)-2, int(index))
        fraction = index-lower
        left, right = samples[lower], samples[lower+1]
        return slerp(left["q"], right["q"], fraction), tuple(x+(y-x)*fraction for x, y in zip(left["a"], right["a"]))

    def sample(self, time_s):
        return self._sample(time_s, self.samples)

    def _sample(self, time_s, samples):
        # This mapping is periodic and continuous at both sides of the seam.
        position = time_s % self.period
        if position >= self.overlap:
            return self.interpolate(position, samples)
        left = self.interpolate(self.period+position, samples)
        right = self.interpolate(position, samples)
        phase = position/self.overlap
        weight = phase*phase*(3-2*phase)
        return slerp(left[0], right[0], weight), tuple(x+(y-x)*weight for x, y in zip(left[1], right[1]))

    def sample_aligned(self, time_s, target_up_axis):
        """Align each source knot before interpolation; preserve legacy sample()."""
        if len(target_up_axis) != 3:
            raise MotionError("Source alignment requires a three-axis target")
        target = unit(target_up_axis)
        if target not in self._aligned_cache:
            aligned, errors = [], []
            for sample, source_up in zip(self.samples, self.source_up_axes):
                rotation = align_vectors(source_up, target)
                aligned.append({"q": unit(qmul(qmul(rotation, sample["q"]), conjugate(rotation))),
                                "a": rotate(rotation, sample["a"])})
                mapped_up = rotate(rotation, source_up)
                errors.append(math.degrees(math.acos(max(-1.0, min(1.0, sum(a*b for a, b in zip(mapped_up, target)))))))
            self._aligned_cache[target] = aligned
            self.metadata["source_alignment"]["targets"].append({
                "target_up_axis": target,
                "max_knot_up_alignment_error_deg": max(errors),
                "residual_metric_scope": "Up-vector rotation at resampled source knots only",
            })
        return self._sample(time_s, self._aligned_cache[target])
