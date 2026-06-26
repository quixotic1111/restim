"""
SwitchingAlgorithm: a RemoteGenerationAlgorithm proxy that delegates to
one of two child algorithms based on its current mode.

Used by the calibration wizard to hot-swap between the user's normal
algorithm and a calibration algorithm without restarting the device.
The active child's parameter_dict() is what the device sees.
"""

from __future__ import annotations

from stim_math.audio_gen.base_classes import RemoteGenerationAlgorithm


class SwitchingAlgorithm(RemoteGenerationAlgorithm):
    MODE_USER = "user"
    MODE_CALIBRATION = "calibration"

    def __init__(
        self,
        user_algorithm: RemoteGenerationAlgorithm,
        calibration_algorithm: RemoteGenerationAlgorithm,
    ):
        super().__init__()
        # Both algorithms must report the same channel count, otherwise
        # mid-flight switching would change the device's expected shape.
        if hasattr(user_algorithm, 'outputs') and hasattr(calibration_algorithm, 'outputs'):
            if user_algorithm.outputs() != calibration_algorithm.outputs():
                raise ValueError(
                    f"user algorithm has {user_algorithm.outputs()} outputs, "
                    f"calibration algorithm has {calibration_algorithm.outputs()}; "
                    f"they must match"
                )
        self.user_algorithm = user_algorithm
        self.calibration_algorithm = calibration_algorithm
        self._mode = self.MODE_USER

    def outputs(self) -> int:
        return self.user_algorithm.outputs()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in (self.MODE_USER, self.MODE_CALIBRATION):
            raise ValueError(f"unknown mode {mode!r}")
        self._mode = mode

    def parameter_dict(self) -> dict:
        if self._mode == self.MODE_CALIBRATION:
            return self.calibration_algorithm.parameter_dict()
        return self.user_algorithm.parameter_dict()
