"""Acoustic-role mesh sizing and pre-mesh size/cost prediction.

This module is shared by the gmsh-side preparation script
(``prepare_step_for_wg_metal.py``) and the Fusion add-in dialog
(``fusion-addins/WGMetalPipeline``). It is intentionally pure Python with no
gmsh/numpy/Fusion imports so the Fusion embedded interpreter can import it for
the live size/cost readout while the dials change.

Two ideas drive it:

* **Size by acoustic role at a frequency-derived target.** A boundary element
  carries the field accurately when it is small against the wavelength. The
  per-role element size is ``min(mm_knob, c / (epw_role * f_max))``: the mm knob
  is an explicit hand ceiling, and the frequency term is the physical floor for
  the requested band top. Radiating surfaces (the waveguide flare and the
  source itself) get the finest target, shadowed rear/outer/far-cabinet
  surfaces ride near Nyquist, and the baffle/near-field is left to the smooth
  distance-graded fallback rather than coarsened to shadow level. Ported from
  ``hornlab_mesher.geometry.MeshDensity`` (commits ce254e6 + fe45d37).

* **Predict mesh size and solve cost before meshing.** ``N_triangles ~= 2.3 *
  sum_region(A_region / h_region^2)`` (validated constant 2.33 +/- 0.15, ~4%
  mean error across the 260612 mesh-sizing study) lets the dialog show the
  triangle count, the dense BEM matrix RAM (``N^2 * 16`` bytes, complex128),
  a calibrated solve time, and the per-role valid band before gmsh runs. The
  per-region sum matters: a single global ``h`` underpredicts a graded mesh by
  up to ~25 %.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

SPEED_OF_SOUND_M_S = 343.0

# Elements-per-wavelength targets per acoustic role. The throat patch area is
# tiny so a finer target there is nearly free; the radiating flare is the large
# accuracy/size lever; shadowed surfaces sit near the 2 e/w Nyquist floor.
DEFAULT_RADIATING_EPW = 6.0
DEFAULT_THROAT_EPW = 8.0
DEFAULT_SHADOW_EPW = 2.5
# Near-field/baffle is graded by distance from the source, not pinned to a
# role ceiling; this is the e/w used only when reporting its valid band.
DEFAULT_NEARFIELD_EPW = DEFAULT_RADIATING_EPW
# Conservative e/w used to report the validated band of an existing mesh from
# its measured max edge (matches hornlab_mesher.config_builder._mesh_report and
# the historical prepare-step global check).
VALIDATION_EPW = 6.0

ROLE_THROAT = "throat"
ROLE_RADIATING = "radiating"
ROLE_NEAR_FIELD = "near_field"
ROLE_SHADOW = "shadow"
ROLE_SOURCE = "source"  # source patches are radiating by definition

# Triangle-count constant: an (almost) equilateral triangle of edge h covers
# ~0.433 h^2, so a surface of area A holds ~A / 0.433 h^2 = 2.31 A / h^2
# triangles. Calibrated to 2.33 +/- 0.15 on the 260612 study meshes.
TRIANGLES_PER_AREA_OVER_H2 = 2.3

# Dense complex128 BEM matrix: N x N entries of 16 bytes each.
COMPLEX128_BYTES = 16

# Solve-time calibration from the 260612 mesh-sizing study (one frequency, the
# m2-clone quarter model, hornlab-metal-bem native yz+xz symmetry). The matrix
# dimension is the quarter triangle count. The 37665-triangle point measured
# 93 s but at 23 GB it is RAM-bound, so the power-law fit uses the two lower,
# compute-bound points and the cubic bound is anchored at the clean mid point.
SOLVE_CALIBRATION_SEC_PER_FREQ = ((8000.0, 1.0), (28178.0, 21.0))
_CUBIC_ANCHOR = (28178.0, 21.0)

# Dense-matrix RAM feasibility bands (gigabytes). These are hardware-agnostic
# severities; the dialog turns the matrix RAM into a gate before a multi-GB or
# 40 GB blow-up (measured: 8k tris -> 1.1 GB, 28k -> 12.7 GB, 38k -> 23 GB).
RAM_CAUTION_GB = 8.0
RAM_WARN_GB = 24.0
RAM_INFEASIBLE_GB = 40.0


def frequency_ceiling_mm(
    epw: float,
    f_max_hz: float | None,
    *,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> float | None:
    """Largest element (mm) that resolves ``f_max_hz`` at ``epw`` elements/wave.

    Returns ``None`` when no band top is requested (``f_max_hz`` falsy/<=0), so
    callers fall back to the mm knob alone.
    """
    if not f_max_hz or f_max_hz <= 0.0:
        return None
    epw = max(float(epw), 1.0)
    return (float(speed_of_sound_m_s) * 1000.0) / (epw * float(f_max_hz))


def valid_f_max_hz(
    size_mm: float,
    *,
    epw: float = VALIDATION_EPW,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> float:
    """Highest frequency an element of ``size_mm`` resolves at ``epw`` e/w."""
    if size_mm <= 0.0:
        return float("inf")
    epw = max(float(epw), 1.0)
    return (float(speed_of_sound_m_s) * 1000.0) / (epw * float(size_mm))


def role_epw(
    role: str,
    *,
    radiating_epw: float = DEFAULT_RADIATING_EPW,
    shadow_epw: float = DEFAULT_SHADOW_EPW,
    throat_epw: float | None = None,
    nearfield_epw: float | None = None,
) -> float:
    """Elements-per-wavelength target for an acoustic role."""
    if role in (ROLE_THROAT,):
        return float(throat_epw) if throat_epw is not None else max(radiating_epw, DEFAULT_THROAT_EPW)
    if role in (ROLE_RADIATING, ROLE_SOURCE):
        return float(radiating_epw)
    if role == ROLE_SHADOW:
        return float(shadow_epw)
    if role == ROLE_NEAR_FIELD:
        return float(nearfield_epw) if nearfield_epw is not None else float(radiating_epw)
    return float(radiating_epw)


def role_size_mm(
    role: str,
    *,
    f_max_hz: float | None,
    mm_knob_mm: float,
    radiating_epw: float = DEFAULT_RADIATING_EPW,
    shadow_epw: float = DEFAULT_SHADOW_EPW,
    throat_epw: float | None = None,
    nearfield_epw: float | None = None,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> float:
    """Planned element size (mm) for a role: ``min(mm_knob, freq ceiling)``.

    The mm knob is always an upper bound, so a coarse hand setting can never be
    overridden into a finer mesh by the frequency term; the frequency term only
    refines where the band requires it.
    """
    epw = role_epw(
        role,
        radiating_epw=radiating_epw,
        shadow_epw=shadow_epw,
        throat_epw=throat_epw,
        nearfield_epw=nearfield_epw,
    )
    ceiling = frequency_ceiling_mm(epw, f_max_hz, speed_of_sound_m_s=speed_of_sound_m_s)
    if ceiling is None:
        return float(mm_knob_mm)
    return min(float(mm_knob_mm), float(ceiling))


def graded_size_mm(
    distance_mm: float,
    *,
    size_min_mm: float,
    size_max_mm: float,
    dist_min_mm: float = 0.0,
    dist_max_mm: float,
) -> float:
    """Linear distance threshold field, matching gmsh ``Threshold``.

    Mirrors the size a gmsh Distance/Threshold field assigns at ``distance_mm``
    from a source patch, so the pre-mesh predictor can evaluate the same
    near-field fallback the mesher will use.
    """
    if dist_max_mm <= dist_min_mm:
        return float(size_min_mm if distance_mm <= dist_min_mm else size_max_mm)
    if distance_mm <= dist_min_mm:
        return float(size_min_mm)
    if distance_mm >= dist_max_mm:
        return float(size_max_mm)
    frac = (distance_mm - dist_min_mm) / (dist_max_mm - dist_min_mm)
    return float(size_min_mm + frac * (size_max_mm - size_min_mm))


@dataclass(frozen=True)
class Region:
    """One area/size pair fed to the triangle-count estimator.

    ``label`` groups regions for per-role reporting; ``role`` records the
    acoustic role for the per-role valid-band readout.
    """

    area_mm2: float
    size_mm: float
    label: str = ""
    role: str = ""

    def triangle_count(self) -> float:
        if self.size_mm <= 0.0 or self.area_mm2 <= 0.0:
            return 0.0
        return TRIANGLES_PER_AREA_OVER_H2 * self.area_mm2 / (self.size_mm * self.size_mm)


def estimate_triangle_count(regions: Iterable[Region | Sequence[float]]) -> int:
    """``N ~= 2.3 * sum(A / h^2)`` over regions; rounds to a whole count.

    Accepts ``Region`` objects or bare ``(area_mm2, size_mm)`` pairs. The sum
    is per region by design: a graded mesh evaluated at a single global ``h``
    underpredicts by up to ~25 %.
    """
    total = 0.0
    for region in regions:
        if isinstance(region, Region):
            total += region.triangle_count()
        else:
            area_mm2, size_mm = float(region[0]), float(region[1])
            if size_mm > 0.0 and area_mm2 > 0.0:
                total += TRIANGLES_PER_AREA_OVER_H2 * area_mm2 / (size_mm * size_mm)
    return int(round(total))


def _solve_power_law() -> tuple[float, float]:
    """Fit ``T = C * N^p`` through the two compute-bound calibration points."""
    (n0, t0), (n1, t1) = SOLVE_CALIBRATION_SEC_PER_FREQ
    import math

    p = math.log(t1 / t0) / math.log(n1 / n0)
    c = t0 / (n0 ** p)
    return c, p


def solve_seconds_per_freq(n_triangles: int) -> float:
    """Calibrated dense-solve wall time per frequency for ``n_triangles``."""
    if n_triangles <= 0:
        return 0.0
    c, p = _solve_power_law()
    return float(c * (n_triangles ** p))


def solve_seconds_cubic_upper(n_triangles: int) -> float:
    """Conservative ``O(N^3)`` per-frequency upper bound (dense LU scaling)."""
    if n_triangles <= 0:
        return 0.0
    n_anchor, t_anchor = _CUBIC_ANCHOR
    c3 = t_anchor / (n_anchor ** 3)
    return float(c3 * (n_triangles ** 3))


def matrix_ram_bytes(n_triangles: int) -> int:
    """Dense complex128 BEM matrix RAM in bytes: ``N^2 * 16``."""
    if n_triangles <= 0:
        return 0
    return int(n_triangles) * int(n_triangles) * COMPLEX128_BYTES


def feasibility_from_ram_gb(ram_gb: float) -> str:
    """Severity label for a dense-matrix RAM footprint."""
    if ram_gb >= RAM_INFEASIBLE_GB:
        return "infeasible"
    if ram_gb >= RAM_WARN_GB:
        return "warn"
    if ram_gb >= RAM_CAUTION_GB:
        return "caution"
    return "ok"


@dataclass(frozen=True)
class MeshCostEstimate:
    n_triangles: int
    ram_bytes: int
    ram_gb: float
    solve_seconds_per_freq: float
    solve_seconds_total: float
    solve_seconds_cubic_upper_per_freq: float
    freq_count: int
    feasibility: str
    per_role_triangles: dict[str, int] = field(default_factory=dict)
    per_role_valid_f_max_hz: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_triangles": int(self.n_triangles),
            "ram_bytes": int(self.ram_bytes),
            "ram_gb": round(float(self.ram_gb), 3),
            "solve_seconds_per_freq": round(float(self.solve_seconds_per_freq), 3),
            "solve_seconds_total": round(float(self.solve_seconds_total), 1),
            "solve_seconds_cubic_upper_per_freq": round(
                float(self.solve_seconds_cubic_upper_per_freq), 3
            ),
            "freq_count": int(self.freq_count),
            "feasibility": self.feasibility,
            "per_role_triangles": {k: int(v) for k, v in self.per_role_triangles.items()},
            "per_role_valid_f_max_hz": {
                k: round(float(v), 1) for k, v in self.per_role_valid_f_max_hz.items()
            },
        }


def estimate_mesh_cost(
    regions: Iterable[Region | Sequence[float]],
    *,
    freq_count: int = 1,
    validation_epw: float = VALIDATION_EPW,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> MeshCostEstimate:
    """Full pre-mesh prediction: triangles, RAM, solve time, per-role bands.

    ``regions`` are summed for the total triangle count; ``Region.label`` (or
    ``role``) buckets the per-role counts, and each bucket's coarsest planned
    size sets that role's predicted valid band.
    """
    regions = list(regions)
    per_role_tris: dict[str, float] = {}
    per_role_max_size: dict[str, float] = {}
    for region in regions:
        if isinstance(region, Region):
            key = region.label or region.role or "all"
            tris = region.triangle_count()
            size = region.size_mm
        else:
            key = "all"
            area_mm2, size = float(region[0]), float(region[1])
            tris = (
                TRIANGLES_PER_AREA_OVER_H2 * area_mm2 / (size * size)
                if size > 0.0 and area_mm2 > 0.0
                else 0.0
            )
        per_role_tris[key] = per_role_tris.get(key, 0.0) + tris
        if size > 0.0:
            per_role_max_size[key] = max(per_role_max_size.get(key, 0.0), size)

    n_triangles = int(round(sum(per_role_tris.values())))
    ram_bytes = matrix_ram_bytes(n_triangles)
    ram_gb = ram_bytes / 1.0e9
    per_freq = solve_seconds_per_freq(n_triangles)
    cubic = solve_seconds_cubic_upper(n_triangles)
    return MeshCostEstimate(
        n_triangles=n_triangles,
        ram_bytes=ram_bytes,
        ram_gb=ram_gb,
        solve_seconds_per_freq=per_freq,
        solve_seconds_total=per_freq * max(int(freq_count), 1),
        solve_seconds_cubic_upper_per_freq=cubic,
        freq_count=max(int(freq_count), 1),
        feasibility=feasibility_from_ram_gb(ram_gb),
        per_role_triangles={k: int(round(v)) for k, v in per_role_tris.items()},
        per_role_valid_f_max_hz={
            k: valid_f_max_hz(size, epw=validation_epw, speed_of_sound_m_s=speed_of_sound_m_s)
            for k, size in per_role_max_size.items()
        },
    )
