"""
Calibration math layer.

Pure-Python (no Qt, no hardware) implementation of the calibration profile
contract between restim and Funscript Tools. The JSON file produced here
is the handoff: restim writes it via the calibration wizard, FT reads it
at render time.
"""

from .device_protocol import (
    CalibrationCapabilities,
    CalibrationDeviceProtocol,
    ElectrodePair,
    ReadingCallback,
    SkinResistanceReading,
)
from .impedance import (
    DEFAULT_GAIN_TRIM_CAP,
    apply_to_electrodes,
    compute_gain_trims,
    normalize_to_attenuation_only,
)
from .io import default_path, load, save
from .layout_inference import OPEN_CIRCUIT_THRESHOLD_OHMS, infer_layout
from .perception_curve import (
    build_from_landmarks,
    output_for_perceived,
    perceived_at,
)
from .profile import (
    SCHEMA_VERSION,
    CalibrationProfile,
    Electrode,
    Hardware,
    PerceptionCurve,
    SafeEnvelope,
)
from .session import CalibrationSession
from .validation import ValidationResult, validate

__all__ = [
    # profile
    "SCHEMA_VERSION",
    "CalibrationProfile",
    "Hardware",
    "Electrode",
    "PerceptionCurve",
    "SafeEnvelope",
    # validation
    "ValidationResult",
    "validate",
    # io
    "load",
    "save",
    "default_path",
    # impedance
    "DEFAULT_GAIN_TRIM_CAP",
    "compute_gain_trims",
    "apply_to_electrodes",
    "normalize_to_attenuation_only",
    # perception curve
    "build_from_landmarks",
    "perceived_at",
    "output_for_perceived",
    # layout
    "OPEN_CIRCUIT_THRESHOLD_OHMS",
    "infer_layout",
    # session
    "CalibrationSession",
    # device protocol
    "CalibrationDeviceProtocol",
    "ElectrodePair",
    "SkinResistanceReading",
    "CalibrationCapabilities",
    "ReadingCallback",
]
