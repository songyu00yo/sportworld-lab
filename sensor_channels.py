"""Coupled console inputs and Android orientation math; no device I/O on import."""
import math

from emulator_motion import MotionError, SENSORS, values


COUPLED_SENSORS = (
    "acceleration", "acceleration-uncalibrated",
    "gyroscope", "gyroscope-uncalibrated",
    "magnetic-field", "magnetic-field-uncalibrated", "orientation",
)


def capture_baseline(console, coupled=True):
    sensors = COUPLED_SENSORS if coupled else SENSORS
    return {sensor: values(console, sensor) for sensor in sensors}


def sample_sensor_inputs(sample):
    inputs = {sensor: sample[sensor] for sensor in SENSORS}
    if "magnetic_field" not in sample or "orientation_deg" not in sample:
        return inputs
    # Goldfish accepts three axes and adds uncalibrated-event bias in its HAL.
    # Uncalibrated gyro events also receive a small HAL-generated rate offset.
    inputs.update({
        "acceleration-uncalibrated": sample["acceleration"],
        "gyroscope-uncalibrated": sample["gyroscope"],
        "magnetic-field": sample["magnetic_field"],
        "magnetic-field-uncalibrated": sample["magnetic_field"],
        "orientation": sample["orientation_deg"],
    })
    return {sensor: inputs[sensor] for sensor in COUPLED_SENSORS}


def android_orientation(gravity_body, magnetic_body):
    """Return Android legacy azimuth/pitch/roll in degrees from body-frame vectors.

    Uses SensorManager's east = magnetic cross gravity and north = up cross
    east construction. At pitch +/-90 degrees azimuth and roll are ambiguous,
    so the same finite atan2 convention as SensorManager is retained.
    """
    if any(len(vector) != 3 or not all(math.isfinite(v) for v in vector)
           for vector in (gravity_body, magnetic_body)):
        raise MotionError("Orientation requires finite three-axis vectors")
    ax, ay, az = gravity_body
    ex, ey, ez = magnetic_body
    gravity_norm = math.sqrt(ax*ax + ay*ay + az*az)
    if not math.isfinite(gravity_norm) or gravity_norm < .1:
        raise MotionError("Orientation gravity vector is degenerate")
    hx, hy, hz = ey*az-ez*ay, ez*ax-ex*az, ex*ay-ey*ax
    east_norm = math.sqrt(hx*hx + hy*hy + hz*hz)
    if not math.isfinite(east_norm) or east_norm < .1:
        raise MotionError("Orientation magnetic field is degenerate or parallel to gravity")
    hx, hy, hz = hx/east_norm, hy/east_norm, hz/east_norm
    ax, ay, az = ax/gravity_norm, ay/gravity_norm, az/gravity_norm
    # Android's row-major ENU rotation matrix is [east, north, up].
    north_y = az*hx-ax*hz
    azimuth = math.degrees(math.atan2(hy, north_y)) % 360
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -ay))))
    roll = math.degrees(math.atan2(-ax, az))
    return azimuth, pitch, roll
