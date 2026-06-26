"""
Validation for CalibrationProfile.

Two flavors of check:
- structural: did the JSON have the right shape? (handled in profile.from_dict)
- semantic: are values in range, ordered correctly, monotonic where required?

validate() aggregates issues into a ValidationResult so downstream code can
choose to either reject a profile outright or proceed with logged warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .profile import SCHEMA_VERSION, CalibrationProfile


@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def validate(profile: CalibrationProfile) -> ValidationResult:
    result = ValidationResult()

    _check_schema_version(profile, result)
    _check_electrodes(profile, result)
    _check_perception_curve(profile, result)
    _check_safe_envelope(profile, result)
    _check_hardware(profile, result)

    return result


def _check_schema_version(profile: CalibrationProfile, result: ValidationResult) -> None:
    if profile.schema_version > SCHEMA_VERSION:
        result.error(
            f"profile schema_version={profile.schema_version} is newer than "
            f"this restim supports ({SCHEMA_VERSION}). Upgrade restim or recalibrate."
        )
    elif profile.schema_version < SCHEMA_VERSION:
        result.warn(
            f"profile schema_version={profile.schema_version} is older than "
            f"current ({SCHEMA_VERSION}); missing fields use defaults."
        )


def _check_electrodes(profile: CalibrationProfile, result: ValidationResult) -> None:
    if not profile.electrodes:
        result.error("profile has no electrode entries")
        return
    for name, e in profile.electrodes.items():
        # Validate on |Z| rather than Z_real_ohms. Firmware measurement noise
        # can produce small negative Z_real values that are physically
        # meaningless but numerically valid as part of a positive-|Z| signal.
        # All downstream consumers (gain_trim math, layout inference, AGC)
        # use |Z| anyway, so negative real parts are harmless data.
        if e.Z_magnitude <= 0:
            result.error(
                f"electrode {name}: |Z| must be > 0 (got |Z|={e.Z_magnitude})"
            )
        if e.gain_trim <= 0:
            result.error(f"electrode {name}: gain_trim must be > 0 (got {e.gain_trim})")


def _check_perception_curve(profile: CalibrationProfile, result: ValidationResult) -> None:
    pc = profile.perception_curve
    if len(pc.output_levels) != len(pc.perceived_intensity):
        result.error(
            f"perception_curve arrays mismatched: "
            f"{len(pc.output_levels)} output_levels vs "
            f"{len(pc.perceived_intensity)} perceived_intensity"
        )
        return
    if len(pc.output_levels) < 2:
        result.error(
            f"perception_curve needs at least 2 points (got {len(pc.output_levels)})"
        )
        return

    for i, x in enumerate(pc.output_levels):
        if not 0.0 <= x <= 1.0:
            result.error(f"perception_curve.output_levels[{i}]={x} out of [0,1]")
    for i, y in enumerate(pc.perceived_intensity):
        if not 0.0 <= y <= 1.0:
            result.error(f"perception_curve.perceived_intensity[{i}]={y} out of [0,1]")

    if any(b < a for a, b in zip(pc.output_levels, pc.output_levels[1:])):
        result.error("perception_curve.output_levels must be non-decreasing")
    if any(b < a for a, b in zip(pc.perceived_intensity, pc.perceived_intensity[1:])):
        result.error("perception_curve.perceived_intensity must be non-decreasing")


def _check_safe_envelope(profile: CalibrationProfile, result: ValidationResult) -> None:
    env = profile.safe_envelope
    for name, value in (
        ("min_useful_output", env.min_useful_output),
        ("preferred_target", env.preferred_target),
        ("max_comfortable_output", env.max_comfortable_output),
    ):
        if not 0.0 <= value <= 1.0:
            result.error(f"safe_envelope.{name}={value} out of [0,1]")

    if not (env.min_useful_output < env.preferred_target < env.max_comfortable_output):
        result.error(
            f"safe_envelope ordering: must have min < preferred < max "
            f"(got {env.min_useful_output} / {env.preferred_target} / "
            f"{env.max_comfortable_output})"
        )


def _check_hardware(profile: CalibrationProfile, result: ValidationResult) -> None:
    if not 0.0 <= profile.hardware.layout_confidence <= 1.0:
        result.error(
            f"hardware.layout_confidence={profile.hardware.layout_confidence} out of [0,1]"
        )
