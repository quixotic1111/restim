"""
Atomic JSON read/write for CalibrationProfile.

Default path is ~/.restim/calibration.json — the same location Funscript
Tools will look at first when discovering a profile. Writes go through a
temp file + atomic rename so a crash mid-write cannot corrupt the live
profile. A single previous version is rotated to calibration.json.bak.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .profile import CalibrationProfile
from .validation import ValidationResult, validate

logger = logging.getLogger('restim.calibration.io')


def default_path() -> Path:
    """Canonical location for the active calibration profile."""
    return Path.home() / ".restim" / "calibration.json"


def load(path: Path | None = None) -> tuple[CalibrationProfile | None, ValidationResult]:
    """Read a profile from disk.

    Returns (profile, result). If the file is missing or unreadable, profile
    is None. If the file parses but values are invalid, profile is returned
    anyway with result.ok=False so the caller can decide.
    """
    path = Path(path) if path else default_path()
    result = ValidationResult()

    if not path.exists():
        result.error(f"profile not found at {path}")
        return None, result

    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        result.error(f"cannot read {path}: {e}")
        return None, result

    try:
        profile = CalibrationProfile.from_dict(data)
    except (TypeError, KeyError) as e:
        result.error(f"profile shape invalid: {e}")
        return None, result

    semantic = validate(profile)
    result.errors.extend(semantic.errors)
    result.warnings.extend(semantic.warnings)
    if semantic.errors:
        result.ok = False

    return profile, result


def save(profile: CalibrationProfile, path: Path | None = None) -> None:
    """Write profile to disk atomically. Refuses to write an invalid profile."""
    path = Path(path) if path else default_path()

    semantic = validate(profile)
    if not semantic.ok:
        raise ValueError(
            f"refusing to save invalid profile: {'; '.join(semantic.errors)}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            os.replace(path, bak)
        except OSError as e:
            logger.warning(f"could not rotate {path} to {bak}: {e}")

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(profile.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
