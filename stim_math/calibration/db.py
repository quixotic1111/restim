"""Linear gain trim → the dB value the CALIBRATION_4_* axes actually honour.

The axes take dB, and every version of this code assumed the firmware honours
them literally. It does not.

MEASURED 2026-09-02 on the FOC-stim, isolated single-channel steps with
baselines bracketing the test (they agreed to 0.9%), scored by comparing the
channel against ITSELF across two windows and dividing by the other three
against themselves — so drive level, drying contact and the shared boost
supply all divide out:

    commanded -3.00 dB -> delivered -6.74 dB   (k = 2.247)
    commanded -4.50 dB -> delivered -9.98 dB   (k = 2.218)

i.e. the device delivers about 2.23 dB per dB commanded. `20*log10(gain)`
therefore attenuates roughly TWICE as hard as intended: a 0.65 trim delivers
0.65^2 = 0.42, which is exactly what a device run measured (E4 at 39% of its
siblings) and had, before this was understood, been read as "E4 couples 40%
weaker".

★Why this was never caught: the conversion was only ever cross-checked against
restim's own startup log, which computes it the same way. Both sides agreed
with each other, and neither was compared against the current the firmware
delivers. Nothing in restim's Python converts the value — it goes straight to
the device as AXIS_CALIBRATION_4_*, so the behaviour lives in firmware.

DB_EXPONENT is 2.0 rather than the fitted 2.23: exactly 2 is what a
power-vs-amplitude dB confusion predicts (the axis is labelled "power [dB]"
and the multiplier lands on current), so it is the principled value, while
2.23 is one device on one evening. The residual is 1-8% across typical trims,
against the ~100% overshoot it replaces.

★★And this is not merely inferred from the measurement. THE CORRECT CONVENTION
WAS ALREADY IN THIS CODEBASE, three files away: the THREE-phase calibration
converts a linear intensity ratio into its "A power"/"B power"/"C power"
spinboxes with `np.log10(x) * 10`, and inverts it with `10 ** (calib / 10)`
(qt_ui/three_phase_settings_widget.py:47,162-164). Same conceptual quantity,
same spinbox labels, ten rather than twenty. The four-phase path simply used
the wrong one of two conventions already present, and the measured 2.23 is
20/10 plus scatter.
"""
import math

#: dB delivered per dB commanded on the CALIBRATION_4_* axes.
DB_EXPONENT = 2.0

#: Trims are for BALANCING, and the felt dynamic range is only ~6-10 dB, so a
#: trim past -9 dB DELIVERED does not balance an electrode, it deletes it
#: (2026-07-08: a saved 0.065 trim made E4 vanish entirely). Expressed as a
#: GAIN so the floor means the same thing whatever DB_EXPONENT turns out to
#: be — clamping the dB number instead would silently double the strictness
#: the moment the exponent changed.
MIN_TRIM_GAIN = 10.0 ** (-9.0 / 20.0)          # 0.355


def gain_to_db(gain: float, exponent: float = DB_EXPONENT) -> float:
    """Linear trim multiplier → the dB to put on the wire.

    Not the textbook dB of that gain — the number that makes the device
    DELIVER that gain, which is the textbook value divided by ``exponent``.
    """
    g = min(max(float(gain), 1e-3), 1.0)
    return 20.0 * math.log10(g) / max(float(exponent), 1e-6)


def db_to_gain(db: float, exponent: float = DB_EXPONENT) -> float:
    """Inverse of :func:`gain_to_db` — what a commanded dB actually delivers."""
    return 10.0 ** (float(exponent) * float(db) / 20.0)
