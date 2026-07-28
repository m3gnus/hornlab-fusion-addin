"""Shared crossover filters for Fusion WG Metal solve and comparison scripts."""

from __future__ import annotations

import numpy as np


def _lr4_lowpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return 1.0 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def _lr4_highpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return (s * s) ** 2 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def crossover_weights(
    freqs: np.ndarray,
    members: list[str],
    crossovers_hz: list[float],
) -> dict[str, np.ndarray]:
    """Return LR4 weights along an ordered driver chain: LP, BP..., HP."""
    weights: dict[str, np.ndarray] = {}
    for index, name in enumerate(members):
        weight = np.ones(np.asarray(freqs).shape, dtype=np.complex128)
        if index > 0:
            weight = weight * _lr4_highpass(freqs, crossovers_hz[index - 1])
        if index < len(crossovers_hz):
            weight = weight * _lr4_lowpass(freqs, crossovers_hz[index])
        weights[name] = weight
    return weights
