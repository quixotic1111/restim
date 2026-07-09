"""
Impedance-to-gain_trim math.

Given per-electrode complex impedance, compute per-electrode gain_trim so
that equal post-trim drive amplitude produces approximately equal current
through each electrode. Trims are mean-anchored (center around 1.0) and
clamped to a safe range — extreme values usually indicate bad contact
rather than a real calibration need.
"""

from __future__ import annotations

from .profile import Electrode

DEFAULT_GAIN_TRIM_CAP = 3.0  # trims outside [1/cap, cap] are clamped + warned


def compute_gain_trims(
    impedances: dict[str, complex],
    cap: float = DEFAULT_GAIN_TRIM_CAP,
) -> tuple[dict[str, float], list[str]]:
    """Return (trims, warnings).

    trims is a {electrode_name: gain_trim_float} dict. The reference is the
    mean |Z| across all electrodes, so trims center around 1.0. Trims that
    would exceed `cap` (or fall below 1/cap) are clamped and a warning is
    appended for the caller to surface.
    """
    if not impedances:
        return {}, ["no impedance readings provided"]

    magnitudes = {name: abs(z) for name, z in impedances.items()}

    nonpos = [(n, m) for n, m in magnitudes.items() if m <= 0]
    if nonpos:
        return {}, [f"electrode {n}: |Z|={m} is non-positive" for n, m in nonpos]

    # A passive load cannot have negative real impedance — such a reading
    # is a measurement failure (bad contact, cross-channel leakage, sense
    # phase error), not data. Leave that electrode untrimmed and keep its
    # garbage out of the reference so it can't poison the others' trims.
    # (Found 2026-07-08: E3 measured Z_real = -127 Ω and was silently used.)
    invalid = {n for n, z in impedances.items() if z.real <= 0}
    warnings: list[str] = [
        f"electrode {n}: non-physical reading (Z_real={impedances[n].real:.0f} Ω"
        f" ≤ 0) — check contact/lead; left untrimmed"
        for n in sorted(invalid)
    ]
    valid_mags = {n: m for n, m in magnitudes.items() if n not in invalid}
    if not valid_mags:
        return ({n: 1.0 for n in magnitudes},
                warnings + ["no physically-valid readings; all trims neutral"])

    reference = sum(valid_mags.values()) / len(valid_mags)

    trims: dict[str, float] = {}
    floor = 1.0 / cap
    for name, mag in magnitudes.items():
        if name in invalid:
            trims[name] = 1.0
            continue
        trim = mag / reference
        if trim > cap:
            warnings.append(
                f"electrode {name}: trim {trim:.2f} clamped to {cap:.2f} "
                f"(|Z|={mag:.0f} vs reference {reference:.0f}). Bad contact likely."
            )
            trim = cap
        elif trim < floor:
            warnings.append(
                f"electrode {name}: trim {trim:.2f} clamped to {floor:.2f} "
                f"(|Z|={mag:.0f} vs reference {reference:.0f})."
            )
            trim = floor
        trims[name] = trim

    return trims, warnings


def compute_trims_from_currents(
    currents: dict[str, float],
    cap: float = DEFAULT_GAIN_TRIM_CAP,
    current_floor: float = 5e-4,
) -> tuple[dict[str, float], list[str]]:
    """Return (trims, warnings) from MEASURED per-electrode current.

    Where compute_gain_trims() infers balance from impedance ratios, this uses
    the current the device actually delivered under an equal/balanced drive
    (the Phase-1 measurement runs all electrodes at 0.25 each with neutral
    trims, so the readings reflect each electrode's true admittance).

    To equalise delivered current, trim_i ∝ 1 / current_i. The result is
    normalised attenuation-only — the weakest electrode anchors at 1.0× and
    everything else sits below it (so the live pipeline never has to amplify
    past its clean range). Electrodes below `current_floor` (amps) are treated
    as near-silent and left at 1.0× rather than divided into. Trims that would
    fall below 1/cap are clamped + warned: that much imbalance is a physical
    contact/impedance problem, not a software fix.
    """
    if not currents:
        return {}, ["no current readings provided"]

    live = {n: I for n, I in currents.items() if I > current_floor}
    if not live:
        return {}, ["all electrodes near zero current; cannot balance from "
                    "measured current (check electrode contact)"]

    reference = min(live.values())          # weakest live electrode → trim 1.0
    floor = 1.0 / cap
    trims: dict[str, float] = {}
    warnings: list[str] = []
    for name, I in currents.items():
        if I <= current_floor:
            trims[name] = 1.0
            warnings.append(
                f"electrode {name}: current {I * 1000:.2f} mA near zero — "
                f"leaving trim at 1.0 (check contact/cable)."
            )
            continue
        trim = reference / I
        if trim < floor:
            warnings.append(
                f"electrode {name}: trim {trim:.2f} clamped to {floor:.2f} — "
                f"delivers ~{I / reference:.1f}× the weakest electrode's current. "
                f"Likely a contact/impedance imbalance to fix physically."
            )
            trim = floor
        trims[name] = trim

    return trims, warnings


def apply_to_electrodes(
    electrodes: dict[str, Electrode],
    trims: dict[str, float],
) -> None:
    """Update electrode.gain_trim in-place from a {name: trim} dict."""
    for name, trim in trims.items():
        if name in electrodes:
            electrodes[name].gain_trim = trim


def normalize_to_attenuation_only(trims: dict[str, float]) -> dict[str, float]:
    """Scale all trims so the loudest is exactly 1.0× (0 dB), others below.

    Preserves the *relative* balance between electrodes (same proportions)
    while guaranteeing no electrode is amplified beyond its natural level.
    This prevents per-electrode signal clipping when the trims are applied
    in restim's signal pipeline — boosting an electrode above 1.0× can push
    the firmware past its clean operating range and produce spotty output.

    Users compensate for the lower overall amplitude by turning up the
    FOC-stim's physical volume knob or restim's master volume slider.

    No-op when all trims are already <= 1.0×.
    """
    if not trims:
        return {}
    max_trim = max(trims.values())
    if max_trim <= 1.0 or max_trim <= 0:
        return dict(trims)
    return {name: trim / max_trim for name, trim in trims.items()}
