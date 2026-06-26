"""
Layout inference from per-electrode impedance.

Coarse heuristic based on the |Z| vector alone (no pair-driven matrix
available with the current firmware). High confidence (>0.8) only for
gross detections: an electrode missing, or a clear disconnect pattern.
Otherwise confidence is low and callers should defer to user override.

OPEN_CIRCUIT_THRESHOLD_OHMS comes from empirical Test 5 (134 kΩ peak on
disconnect, body range stays well below 10 kΩ).
"""

from __future__ import annotations

OPEN_CIRCUIT_THRESHOLD_OHMS = 50_000


def infer_layout(impedances: dict[str, complex]) -> tuple[str, float]:
    """Return (layout_name, confidence ∈ [0,1])."""
    if not impedances:
        return "", 0.0

    magnitudes = {name: abs(z) for name, z in impedances.items()}
    connected = {n: m for n, m in magnitudes.items() if m < OPEN_CIRCUIT_THRESHOLD_OHMS}
    n_connected = len(connected)
    n_total = len(magnitudes)

    if n_connected == 0:
        return "no_contact", 1.0

    if n_connected < n_total:
        if n_connected == 3 and n_total == 4:
            return "three_phase", 0.85
        return "partial", 0.7

    # All electrodes connected. With only per-electrode |Z|, we can detect
    # gross imbalance but not distinguish specific layouts.
    values = list(connected.values())
    mean = sum(values) / len(values)
    if mean <= 0:
        return "", 0.0
    max_dev = max(abs(v - mean) / mean for v in values)

    if max_dev > 0.5:
        return "asymmetric_quad", 0.4

    return "balanced_quad", 0.4
