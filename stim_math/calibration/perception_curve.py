"""
Perception curve math.

Curves map output level (0..1) to perceived intensity (0..1), built from
the wizard's 3-landmark sweep. Both forward and inverse mappings are
supported — FT uses the inverse to translate "I want X perceived" AGC
targets into actual drive levels.

Implementation is piecewise linear (no scipy dependency, matching the
rest of stim_math). Three landmarks give four knots once the (0, 0)
anchor is added, which is plenty of resolution for AGC translation.
"""

from __future__ import annotations

import bisect

from .profile import PerceptionCurve, SafeEnvelope

# Conventions for the 3-landmark sweep — perceived intensity at each landmark
PERCEIVED_AT_JUST_FEEL = 0.05    # "barely noticeable"
PERCEIVED_AT_COMFORTABLE = 0.50  # midpoint of the comfortable range
PERCEIVED_AT_MAX = 1.00          # top of the user's stated comfort range


def build_from_landmarks(
    just_feel: float,
    comfortable: float,
    max_comfortable: float,
) -> tuple[PerceptionCurve, SafeEnvelope]:
    """Construct (curve, envelope) from the wizard's 3 landmark taps.

    Inputs are output levels (0..1) where the user marked each landmark
    during the slow ramp.
    """
    if not (0.0 < just_feel < comfortable < max_comfortable <= 1.0):
        raise ValueError(
            f"landmarks must satisfy 0 < just_feel ({just_feel}) "
            f"< comfortable ({comfortable}) < max ({max_comfortable}) <= 1.0"
        )

    curve = PerceptionCurve(
        output_levels=[0.0, just_feel, comfortable, max_comfortable],
        perceived_intensity=[0.0, PERCEIVED_AT_JUST_FEEL, PERCEIVED_AT_COMFORTABLE, PERCEIVED_AT_MAX],
    )
    envelope = SafeEnvelope(
        min_useful_output=just_feel,
        preferred_target=comfortable,
        max_comfortable_output=max_comfortable,
    )
    return curve, envelope


def perceived_at(curve: PerceptionCurve, output: float) -> float:
    """Forward: output (0..1) → perceived intensity (0..1). Clamps to domain."""
    output = _clamp01(output)
    return _linear_interp(curve.output_levels, curve.perceived_intensity, output)


def output_for_perceived(curve: PerceptionCurve, perceived: float) -> float:
    """Inverse: perceived (0..1) → output (0..1). Clamps to range."""
    perceived = _clamp01(perceived)
    return _linear_interp(curve.perceived_intensity, curve.output_levels, perceived)


def _linear_interp(xs: list[float], ys: list[float], x: float) -> float:
    """Piecewise linear. xs must be non-decreasing and same length as ys."""
    if not xs:
        return 0.0
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_left(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))
