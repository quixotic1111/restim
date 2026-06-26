"""
CalibrationSession: in-progress wizard state.

Holds intermediate measurements as each phase populates it. finalize()
turns the session into a CalibrationProfile. Partial state can be saved
after Phase 4 so a power-cycle abort in Phases 5-7 doesn't lose the
user's perception data.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .impedance import (
    compute_gain_trims,
    compute_trims_from_currents,
    normalize_to_attenuation_only,
)
from .layout_inference import infer_layout
from .perception_curve import build_from_landmarks
from .profile import CalibrationProfile, Electrode, ElectrodeTilt, Hardware

logger = logging.getLogger('restim.calibration.session')


def _partial_path() -> Path:
    return Path.home() / '.restim' / 'calibration.partial.json'


@dataclass
class CalibrationSession:
    restim_version: str = ""
    user_label: str = "default"
    device_name: str = ""

    # Phase 1: per-electrode complex impedance + derived trims
    impedances: dict[str, complex] = field(default_factory=dict)
    gain_trims: dict[str, float] = field(default_factory=dict)

    # Phase 1 (optional): MEASURED per-electrode RMS current in amps, captured
    # from the device's NotificationCurrents during the same balanced drive that
    # measures impedance. Empty if the firmware/telemetry didn't report it.
    # Phase 3's "Auto-balance from measured current" turns this into trims.
    measured_currents: dict[str, float] = field(default_factory=dict)

    # Phase 2: layout inference result
    layout: str = ""
    layout_confidence: float = 0.0
    layout_user_override: bool = False

    # Phase 4: landmark taps (output levels, 0..1)
    landmark_just_feel: float | None = None
    landmark_comfortable: float | None = None
    landmark_max: float | None = None

    # Phase 6 (tilt): per-electrode spectral tilt in dB (optional — all zeros = no tilt)
    tilt_db: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    notes: str = ""

    # === Phase-specific record helpers ===

    def record_impedances(self, impedances: dict[str, complex]) -> list[str]:
        """Phase 1: store readings and compute gain_trims. Returns any warnings."""
        self.impedances = dict(impedances)
        trims, warnings = compute_gain_trims(impedances)
        self.gain_trims = trims
        return warnings

    def record_measured_currents(self, currents: dict[str, float]) -> None:
        """Phase 1 (optional): store measured per-electrode RMS current (amps).

        Does not touch gain_trims — the measurement only becomes a trim when the
        user opts in via Phase 3's auto-balance button (see trims_from_currents).
        """
        self.measured_currents = {n: float(i) for n, i in currents.items()}

    def trims_from_currents(self) -> tuple[dict[str, float], list[str]]:
        """Phase 3: per-electrode trims that equalise the MEASURED current.

        Returns ({} , [reason]) when no current was captured, so the caller can
        keep the auto-balance button disabled."""
        if not self.measured_currents:
            return {}, ["no measured current available (older firmware or "
                        "telemetry off during Phase 1)"]
        return compute_trims_from_currents(self.measured_currents)

    def record_layout(self, name: str, confidence: float, user_picked: bool) -> None:
        """Phase 2 result (or override)."""
        self.layout = name
        self.layout_confidence = confidence
        self.layout_user_override = user_picked

    def infer_and_record_layout(self) -> tuple[str, float]:
        """Phase 2: auto-infer + record without override. Returns (name, conf)."""
        name, conf = infer_layout(self.impedances)
        self.record_layout(name, conf, user_picked=False)
        return name, conf

    def record_trim_adjustments(self, adjustments: dict[str, float]) -> None:
        """Phase 3: multiply through user-tuned adjustment factors."""
        for name, factor in adjustments.items():
            if name in self.gain_trims:
                self.gain_trims[name] *= factor

    def record_landmarks(
        self,
        just_feel: float,
        comfortable: float,
        max_comfortable: float,
    ) -> None:
        """Phase 4 result."""
        self.landmark_just_feel = just_feel
        self.landmark_comfortable = comfortable
        self.landmark_max = max_comfortable

    def record_tilt(self, tilt_db: list[float]) -> None:
        """Phase 6 result: per-electrode spectral tilt in dB (E1..E4 order)."""
        self.tilt_db = [float(v) for v in tilt_db]

    # === Finalization ===

    def has_complete_perception(self) -> bool:
        return (
            self.landmark_just_feel is not None
            and self.landmark_comfortable is not None
            and self.landmark_max is not None
        )

    def finalize(self) -> CalibrationProfile:
        """Build a complete CalibrationProfile from the session state."""
        if not self.impedances:
            raise ValueError(
                'cannot finalize: no impedance readings recorded (Phase 1 missing)'
            )
        if not self.has_complete_perception():
            raise ValueError(
                'cannot finalize: perception landmarks missing (Phase 4 missing)'
            )

        profile = CalibrationProfile.new(
            restim_version=self.restim_version,
            user_label=self.user_label,
        )
        profile.hardware = Hardware(
            device=self.device_name,
            layout=self.layout,
            # If the user picked manually, drop confidence to 0 so consumers
            # know it's a human choice not a measurement.
            layout_confidence=0.0 if self.layout_user_override else self.layout_confidence,
        )
        # Normalize trims so no electrode is boosted beyond 1.0×. The raw
        # session trims (with user adjustments from Phase 3) may exceed 1.0×;
        # this keeps relative balance while ensuring the live signal pipeline
        # never has to amplify per-electrode output past its clean range.
        safe_trims = normalize_to_attenuation_only(self.gain_trims)
        profile.electrodes = {
            name: Electrode(
                Z_real_ohms=z.real,
                Z_imag_ohms=z.imag,
                gain_trim=safe_trims.get(name, 1.0),
            )
            for name, z in self.impedances.items()
        }
        curve, envelope = build_from_landmarks(
            self.landmark_just_feel,
            self.landmark_comfortable,
            self.landmark_max,
        )
        profile.perception_curve = curve
        profile.safe_envelope = envelope
        profile.tilt = ElectrodeTilt(
            tilt_db=list(self.tilt_db),
            hinge_hz=5.0,
        )
        profile.notes = self.notes
        # Provenance: record what each electrode actually delivered (mA) during
        # the Phase-1 measurement, if captured. Makes the profile self-documenting
        # (and lets a later tool compare delivered current vs the trims).
        if self.measured_currents:
            profile.measured_currents_ma = {
                name: round(amps * 1000.0, 3)
                for name, amps in self.measured_currents.items()
            }
        return profile

    # === Partial save / restore (power-cycle recovery) ===

    def save_partial(self, path: Path | None = None) -> None:
        """Persist state for recovery. Only writes if Phase 4 is complete."""
        if not self.has_complete_perception():
            return
        path = path or _partial_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'restim_version': self.restim_version,
            'user_label': self.user_label,
            'device_name': self.device_name,
            'impedances': {n: [z.real, z.imag] for n, z in self.impedances.items()},
            'gain_trims': self.gain_trims,
            'measured_currents': self.measured_currents,
            'layout': self.layout,
            'layout_confidence': self.layout_confidence,
            'layout_user_override': self.layout_user_override,
            'landmark_just_feel': self.landmark_just_feel,
            'landmark_comfortable': self.landmark_comfortable,
            'landmark_max': self.landmark_max,
            'tilt_db': self.tilt_db,
            'notes': self.notes,
        }
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + '.', suffix='.tmp', dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load_partial(cls, path: Path | None = None) -> CalibrationSession | None:
        """Restore from a partial save, or None if not present / unreadable."""
        path = path or _partial_path()
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f'partial session unreadable: {e}')
            return None
        try:
            return cls(
                restim_version=data.get('restim_version', ''),
                user_label=data.get('user_label', 'default'),
                device_name=data.get('device_name', ''),
                impedances={
                    n: complex(v[0], v[1])
                    for n, v in data.get('impedances', {}).items()
                },
                gain_trims=data.get('gain_trims', {}),
                measured_currents=data.get('measured_currents', {}),
                layout=data.get('layout', ''),
                layout_confidence=data.get('layout_confidence', 0.0),
                layout_user_override=data.get('layout_user_override', False),
                landmark_just_feel=data.get('landmark_just_feel'),
                landmark_comfortable=data.get('landmark_comfortable'),
                landmark_max=data.get('landmark_max'),
                tilt_db=data.get('tilt_db', [0.0, 0.0, 0.0, 0.0]),
                notes=data.get('notes', ''),
            )
        except (TypeError, KeyError) as e:
            logger.warning(f'partial session shape invalid: {e}')
            return None

    @staticmethod
    def clear_partial(path: Path | None = None) -> None:
        """Remove the partial save (call after successful finalize+save)."""
        path = path or _partial_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
