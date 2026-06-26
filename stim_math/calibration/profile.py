"""
CalibrationProfile: typed in-memory representation of a calibration.json file.

The file is the contract between restim (which produces it via the wizard)
and Funscript Tools (which consumes it at render time). Schema versioning:
bump SCHEMA_VERSION on incompatible changes. Adding optional fields is
forward-compatible and does not require a bump.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = 1


@dataclass
class Hardware:
    device: str = ""               # device identifier reported by firmware
    layout: str = ""               # known layout name, or "" if user picked manually
    layout_confidence: float = 0.0 # 0..1; 0 means user picked, not inferred


@dataclass
class Electrode:
    """Per-electrode complex impedance and derived trim.

    The full complex impedance Z = Z_real + j*Z_imag is stored so callers
    can derive magnitude, phase, or other measures without information loss.
    """
    Z_real_ohms: float
    Z_imag_ohms: float = 0.0
    gain_trim: float = 1.0

    @property
    def Z(self) -> complex:
        return complex(self.Z_real_ohms, self.Z_imag_ohms)

    @property
    def Z_magnitude(self) -> float:
        return abs(self.Z)


@dataclass
class PerceptionCurve:
    """Maps output level (0..1) to perceived intensity (0..1).

    Two parallel lists, same length, both monotonic non-decreasing.
    A 3-landmark sweep produces 3 points; a stepped rating produces 7+.
    """
    output_levels: list[float] = field(default_factory=list)
    perceived_intensity: list[float] = field(default_factory=list)


@dataclass
class SafeEnvelope:
    min_useful_output: float = 0.0
    preferred_target: float = 0.5
    max_comfortable_output: float = 1.0


@dataclass
class ElectrodeTilt:
    """Per-electrode spectral tilt from the wizard's frequency-response phase.

    tilt_db: four values in [-12, +12] dB (E1..E4 order). Positive = HF
    emphasis (boosts fast transients); negative = LF emphasis (boosts slow
    envelope). Zero = identity (no tilt applied).

    hinge_hz: temporal crossover between slow-envelope and fast-transient
    content. 5 Hz suits typical 50 Hz funscript signals.
    """
    tilt_db: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    hinge_hz: float = 5.0


@dataclass
class CalibrationProfile:
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""           # ISO 8601 UTC, e.g. "2026-05-12T14:30:00Z"
    restim_version: str = ""
    user_label: str = "default"
    hardware: Hardware = field(default_factory=Hardware)
    electrodes: dict[str, Electrode] = field(default_factory=dict)
    perception_curve: PerceptionCurve = field(default_factory=PerceptionCurve)
    safe_envelope: SafeEnvelope = field(default_factory=SafeEnvelope)
    tilt: ElectrodeTilt = field(default_factory=ElectrodeTilt)
    notes: str = ""
    # Provenance: per-electrode RMS current (mA) measured during the Phase-1
    # balanced drive, when the measured-current balance was used. Empty for
    # impedance-only calibrations. Informational — consumers may ignore it.
    measured_currents_ma: dict[str, float] = field(default_factory=dict)

    @classmethod
    def new(cls, restim_version: str = "", user_label: str = "default") -> CalibrationProfile:
        """Create a fresh profile with a UTC timestamp."""
        return cls(
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            restim_version=restim_version,
            user_label=user_label,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CalibrationProfile:
        """Build a profile from a parsed JSON dict.

        Missing fields fall back to defaults, so older profiles read by newer
        code stay usable (forward compatibility). Unknown extra fields are
        ignored — caller should warn via validate() if schema_version differs.
        """
        hardware = Hardware(**data.get("hardware", {}))
        electrodes = {
            k: Electrode(**v)
            for k, v in data.get("electrodes", {}).items()
        }
        perception_curve = PerceptionCurve(**data.get("perception_curve", {}))
        safe_envelope = SafeEnvelope(**data.get("safe_envelope", {}))
        tilt_data = data.get("tilt") or {}
        tilt = ElectrodeTilt(
            tilt_db=list(tilt_data.get("tilt_db", [0.0, 0.0, 0.0, 0.0])),
            hinge_hz=float(tilt_data.get("hinge_hz", 5.0)),
        )
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            created_at=data.get("created_at", ""),
            restim_version=data.get("restim_version", ""),
            user_label=data.get("user_label", "default"),
            hardware=hardware,
            electrodes=electrodes,
            perception_curve=perception_curve,
            safe_envelope=safe_envelope,
            tilt=tilt,
            notes=data.get("notes", ""),
            measured_currents_ma=dict(data.get("measured_currents_ma", {})),
        )
