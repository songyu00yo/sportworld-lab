"""Flat-ground assumption with time-preserving synthetic or digitized altitude."""
import bisect
import copy
import hashlib
import json
import math
from pathlib import Path
import random

from emulator_motion import MotionError

DEFAULT_BASE_M = 79.2
DEFAULT_SOURCE = (
    "User-provided App altitude chart digitized approximately at its original time scale; "
    "not a surveyed terrain elevation or raw GPS samples"
)
HEIGHT_REFERENCE = "User App displayed altitude; vertical datum unverified"
SYNTHETIC_SOURCE = "Synthetic example altitude profile; not a surveyed terrain elevation or observed GPS samples"
SYNTHETIC_HEIGHT_REFERENCE = "Synthetic model altitude; vertical datum unverified"
USER_REFERENCE_PATH = Path(__file__).resolve().parent / '.lab/reference/user-elevation-profile.json'


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_parameters(seed, base_m, max_error_m, source):
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise MotionError("Elevation seed must be an integer")
    if not all(_finite_number(value) for value in (base_m, max_error_m)):
        raise MotionError("Elevation parameters must be finite numbers")
    if not -500 <= base_m <= 9000 or not 0 <= max_error_m <= 20:
        raise MotionError("Elevation parameters outside supported bounds")
    if not isinstance(source, str) or not source.strip():
        raise MotionError("Elevation source must be a nonempty description")


def _reference_hash(reference):
    try:
        encoded = json.dumps(reference, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise MotionError('Elevation reference must contain finite JSON data') from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_reference(reference):
    if not isinstance(reference, dict):
        raise MotionError("Elevation reference must be an object")
    if (type(reference.get('schema_version')) is not int or reference.get('schema_version') != 1
            or reference.get('vertical_datum') != 'unverified'):
        raise MotionError("Elevation reference requires schema version 1 and unverified datum")
    source_kind = reference.get('source_kind', 'digitized_app_chart')
    if source_kind == 'synthetic':
        if reference.get('digitized_approximation') is not False or 'screenshot_sha256' in reference:
            raise MotionError("Synthetic elevation must not claim digitization or a screenshot SHA-256")
    elif source_kind == 'digitized_app_chart':
        if reference.get('digitized_approximation') is not True:
            raise MotionError("Digitized elevation must identify approximate digitization")
        screenshot_hash = reference.get('screenshot_sha256')
        if (not isinstance(screenshot_hash, str) or len(screenshot_hash) != 64
                or any(char not in '0123456789abcdef' for char in screenshot_hash)):
            raise MotionError("Elevation reference requires a screenshot SHA-256")
    else:
        raise MotionError("Unsupported elevation reference source kind")
    if not isinstance(reference.get('source'), str) or not reference['source'].strip():
        raise MotionError("Elevation reference requires source provenance")
    duration = reference.get('original_duration_s')
    bounds = reference.get('displayed_range_m')
    if (not _finite_number(duration) or duration <= 15
            or not isinstance(bounds, (list, tuple)) or len(bounds) != 2
            or not all(_finite_number(value) for value in bounds)
            or bounds[0] >= bounds[1]):
        raise MotionError("Elevation reference has invalid duration or displayed range")
    samples = reference.get('samples')
    if not isinstance(samples, (list, tuple)) or len(samples) < 2:
        raise MotionError("Elevation reference requires at least two samples")
    previous_time = -1
    for sample in samples:
        if (not isinstance(sample, (list, tuple)) or len(sample) != 2
                or not all(_finite_number(value) for value in sample)):
            raise MotionError("Elevation reference samples must contain finite time and altitude")
        time_s, altitude_m = sample
        if not previous_time < time_s <= duration or not bounds[0] <= altitude_m <= bounds[1]:
            raise MotionError("Elevation reference samples must be ordered and inside displayed bounds")
        previous_time = time_s
    if samples[0][0] != 0 or samples[-1][0] != duration:
        raise MotionError("Elevation reference must cover its complete original elapsed-time axis")


class ElevationProfile:
    """Keep terrain flat and replay the reference altitude without time compression."""
    def __init__(self, seed=0, base_m=DEFAULT_BASE_M, max_error_m=3.3, source=None,
                 reference_path=None):
        reference_path = USER_REFERENCE_PATH if reference_path is None else Path(reference_path)
        try:
            reference = json.loads(reference_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise MotionError(f"Cannot read elevation reference {reference_path}: {exc}") from exc
        _validate_reference(reference)
        source_kind = reference.get('source_kind', 'digitized_app_chart')
        synthetic = source_kind == 'synthetic'
        if source is None:
            source = ((SYNTHETIC_SOURCE if synthetic else DEFAULT_SOURCE)
                      if base_m == DEFAULT_BASE_M
                      else "Caller-provided elevation assumption; unsurveyed")
        _validate_parameters(seed, base_m, max_error_m, source)
        self.base_m = float(base_m)
        self.max_error_m = float(max_error_m)
        # A separate generator leaves the existing pace RNG stream untouched.
        source_offset = random.Random(f"elevation-v2:{seed}").uniform(0, 15)
        self.metadata = {
            'model_version': 2,
            'ground_model': 'flat assumption; no terrain survey',
            'base_m': self.base_m,
            'source': source,
            'source_kind': source_kind,
            'height_reference': SYNTHETIC_HEIGHT_REFERENCE if synthetic else HEIGHT_REFERENCE,
            'seed': seed,
            'max_error_m': self.max_error_m,
            'noise_model': ('synthetic time-varying altitude example' if synthetic
                            else 'approximate digitized displayed altitude observation'),
            'source_time_offset_s': source_offset,
            'time_scale': 1.0,
            'out_of_range_policy': 'hold final reference altitude',
            'reference_sha256': _reference_hash(reference),
            'reference_trace': reference,
        }
        self._prepare_reference()

    def _prepare_reference(self):
        reference = self.metadata['reference_trace']
        self._samples = tuple(tuple(sample) for sample in reference['samples'])
        self._times = tuple(sample[0] for sample in self._samples)
        low, high = reference['displayed_range_m']
        self._reference_midpoint = (low + high) / 2
        self._reference_half_range = (high - low) / 2

    @classmethod
    def from_metadata(cls, metadata):
        """Restore recorded v1 waves or a self-contained v2 trace for exact continuation."""
        if not isinstance(metadata, dict):
            raise MotionError("Elevation metadata must be an object")
        metadata = copy.deepcopy(metadata)
        required = ('model_version', 'seed', 'base_m', 'max_error_m', 'source')
        if any(key not in metadata for key in required):
            raise MotionError("Elevation metadata is missing required fields")
        _validate_parameters(metadata['seed'], metadata['base_m'], metadata['max_error_m'],
                             metadata['source'])
        version = metadata['model_version']
        if type(version) is not int or version not in (1, 2):
            raise MotionError("Unsupported elevation model version")
        instance = cls.__new__(cls)
        instance.base_m = float(metadata['base_m'])
        instance.max_error_m = float(metadata['max_error_m'])
        instance.metadata = metadata
        if version == 1:
            waves = metadata.get('waves')
            if not isinstance(waves, (list, tuple)) or not waves:
                raise MotionError("Historical elevation metadata requires its recorded waves")
            for wave in waves:
                if (not isinstance(wave, (list, tuple)) or len(wave) != 3
                        or not all(_finite_number(value) for value in wave)
                        or wave[0] < 0 or wave[1] <= 0):
                    raise MotionError("Historical elevation waves are malformed")
            if sum(wave[0] for wave in waves) > 1 + 1e-12:
                raise MotionError("Historical elevation wave weights exceed the error bound")
            instance.waves = tuple(tuple(wave) for wave in waves)
        else:
            reference = metadata.get('reference_trace')
            _validate_reference(reference)
            if ('source_kind' in metadata and metadata['source_kind'] !=
                    reference.get('source_kind', 'digitized_app_chart')):
                raise MotionError("Elevation metadata source kind does not match its reference")
            if metadata.get('reference_sha256') != _reference_hash(reference):
                raise MotionError("Elevation metadata reference SHA-256 does not match its trace")
            offset = metadata.get('source_time_offset_s')
            if not _finite_number(offset) or not 0 <= offset <= 15:
                raise MotionError("Elevation source-time offset must be between 0 and 15 seconds")
            if (isinstance(metadata.get('time_scale'), bool) or metadata.get('time_scale') != 1
                    or metadata.get('out_of_range_policy') != 'hold final reference altitude'):
                raise MotionError("Elevation metadata must preserve original time scale without cycling")
            instance._prepare_reference()
        return instance

    def at(self, absolute_time_s, arc_m):
        if any(not _finite_number(value) or value < 0 for value in (absolute_time_s, arc_m)):
            raise MotionError("Elevation time and arc must be finite and nonnegative")
        if self.metadata['model_version'] == 1:
            error = self.max_error_m * sum(
                weight * math.sin(2 * math.pi * absolute_time_s / period + phase)
                for weight, period, phase in self.waves
            )
        else:
            source_time = absolute_time_s + self.metadata['source_time_offset_s']
            index = bisect.bisect_right(self._times, source_time)
            if index == len(self._samples):
                observed = self._samples[-1][1]
            else:
                t0, height0 = self._samples[index - 1]
                t1, height1 = self._samples[index]
                observed = height0 + (height1 - height0) * (source_time - t0) / (t1 - t0)
            error = self.max_error_m * (observed - self._reference_midpoint) / self._reference_half_range
        return {'ground_altitude_m': self.base_m, 'altitude_m': self.base_m + error}
