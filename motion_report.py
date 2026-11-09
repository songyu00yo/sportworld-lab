"""Export reproducible offline evidence; no Android or account access."""
import json
import math
import hashlib
from pathlib import Path

from motion_reference import (G, HELDOUT_MEMBER, TRAIN_MEMBER, channel,
                              read_record, ROOT)
from run_trajectory import DEFAULT_GRAVITY, HZ, signal_metrics, verify_samples


def expected_timeline(samples, elapsed_offset=0):
    """One-second summaries aligned to the sender's reporting windows."""
    groups = {}
    for sample in samples:
        bucket = math.ceil(sample["t_s"]-1e-9)
        groups.setdefault(bucket, []).append(sample)
    timeline = []
    for group in groups.values():
        last = group[-1]
        count = len(group)
        acceleration_rms = math.sqrt(sum(sum(v*v for v in sample["acceleration"]) for sample in group)/count)
        gyro_rms = math.sqrt(sum(sum(v*v for v in sample["gyroscope"]) for sample in group)/count)
        timeline.append({"elapsed_s": elapsed_offset+last["t_s"], "distance_m": last["arc_m"],
                         "speed_m_s": last["speed_m_s"],
                         "cadence_spm": sum(sample["cadence_nominal_spm"] for sample in group)/count,
                         "step_length_m": last.get("step_length_m"),
                         "step_count_model": last.get("step_count_model"),
                         "altitude_m": last.get("altitude_m"),
                         "ground_altitude_m": last.get("ground_altitude_m"),
                         "acceleration_rms_m_s2": acceleration_rms, "gyro_rms_rad_s": gyro_rms,
                         "interval_imu_hz": HZ, "lateness_s": 0})
    return timeline


def heldout_signals():
    rows = read_record(HELDOUT_MEMBER)
    accelerations = [tuple(-G*(g+a) for g, a in zip(channel(row, "gravity"), channel(row, "userAcceleration"))) for row in rows]
    gyroscopes = [channel(row, "rotationRate") for row in rows]
    # Match the reference import's anti-aliasing filter and output sample rate.
    weights = (1, 4, 6, 4, 1)
    filtered = []
    for source in (accelerations, gyroscopes):
        smoothed = [tuple(sum(weights[k]*source[i+k-2][j] for k in range(5))/16 for j in range(3))
                    for i in range(2, len(source)-2)]
        output = []
        for tick in range(int((len(smoothed)-1)*HZ/50)):
            position = tick*50/HZ
            lower = int(position)
            fraction = position-lower
            output.append(tuple(a+(b-a)*fraction for a, b in zip(smoothed[lower], smoothed[lower+1])))
        filtered.append(output)
    return filtered


def make_report(samples, metadata, reference):
    checks = verify_samples(samples, metadata)
    heldout_acc, heldout_gyro = heldout_signals()
    # Use equal 30-second excerpts. Very short extension exports are shorter.
    start = min(400, max(0, (len(samples)-3)//4))
    count = min(600, len(samples)-start, len(heldout_acc))
    excerpt = samples[start:start+count]
    gravity_length = math.sqrt(sum(v*v for v in DEFAULT_GRAVITY))
    old_acc, old_gyro = [], []
    for i in range(count):
        wave = math.sin(i/HZ*metadata["cadence_target"]/60*2*math.pi)
        old_acc.append(tuple(v+v/gravity_length*1.5*wave for v in DEFAULT_GRAVITY))
        old_gyro.append((0, 0, .15*wave))
    metrics = {
        "old_fixed_wave": signal_metrics(old_acc, old_gyro, HZ),
        "new_coupled_reference": signal_metrics([s["acceleration"] for s in excerpt], [s["gyroscope"] for s in excerpt], HZ),
        "heldout_public_jogging": signal_metrics(heldout_acc[:count], heldout_gyro[:count], HZ),
    }
    comparisons = {}
    for key in ("acceleration_norm_mean", "acceleration_norm_std", "acceleration_norm_p95",
                "gyro_norm_rms", "spectrum_peak_hz", "spectrum_high_band_fraction",
                "acceleration_gyro_norm_correlation"):
        target = metrics["heldout_public_jogging"][key]
        old = metrics["old_fixed_wave"][key]
        new = metrics["new_coupled_reference"][key]
        if target is None or old is None or new is None:
            comparisons[key] = {"comparison": "undefined"}
            continue
        old_error, new_error = abs(old-target), abs(new-target)
        comparisons[key] = {"old_absolute_difference": old_error, "new_absolute_difference": new_error,
                            "closer_to_this_excerpt": "new" if new_error < old_error else "old" if old_error < new_error else "equal"}
    implementation = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                      for name in ("motion_reference.py", "run_trajectory.py", "elevation_profile.py", "sensor_channels.py", "stadium_track.py", "motion_report.py", "run_session.py", "emulator_route.py", "emulator_motion.py")}
    return {"schema": 1, "evidence_kind": "offline generated data and independent public-subject excerpt",
            "implementation_sha256": implementation,
            "trajectory": metadata, "consistency": checks,
            "reference": reference.metadata, "train_member": TRAIN_MEMBER, "heldout_member": HELDOUT_MEMBER,
            "comparison_excerpt_seconds": count/HZ, "metrics": metrics, "comparisons": comparisons,
            "real_emulator_stream_tested": False, "app_submission_performed": False,
            "limitations": reference.metadata["limitations"] + [
                "Single held-out excerpt; no population accuracy or anti-cheat detection estimate",
                "Generated step cadence is checked from the waveform; App step detection requires separate manual validation",
                "Algebraic consistency tests do not establish human biomechanics",
                "App timing and distance acceptance require a separate observed run"]}


def export_samples(path, samples, report):
    from run_session import atomic_json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = path.with_suffix(path.suffix+".report.json")
    if report_path.exists():
        raise FileExistsError("Report already exists: "+str(report_path))
    # The report is the completion marker. Remove our new data on failure.
    with path.open("x", encoding="utf-8") as output:
        try:
            for sample in samples:
                output.write(json.dumps(sample, ensure_ascii=False, allow_nan=False)+"\n")
            output.flush()
            atomic_json(report_path, report)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return report_path
