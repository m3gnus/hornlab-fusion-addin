#!/usr/bin/env python3
"""Solve a Fusion-exported WG Metal mesh directly with hornlab-metal-bem.

This script is intentionally independent of Waveguide Generator. It consumes
the tagged multi-source mesh from ``prepare_step_for_wg_metal.py`` and solves
one unit-velocity source at a time with an explicit observation frame using
the canonical ``hornlab_metal_bem`` native Metal solver (dense Accelerate
``cgesv`` solve, no iterative fallback).

Phase convention: the native solver returns ``e^{-i omega t}`` phasors
(Green kernel ``e^{+ikr}``; a time delay of tau multiplies the phasor by
``e^{+i omega tau}``). Every post-processing formula in this script — LR4
crossover weights, ``e^{-j omega tau}`` alignment delays, the ``+j omega``
chamber/port branch math from ``hornlab_sim``, FRD phase export — uses the
engineering ``e^{+j omega t}`` convention instead. The conversion happens at
exactly one boundary: pressure NPZ artifacts are conjugated on write and
tagged with a ``phase_convention`` key; legacy untagged files are conjugated
on load (see ``_pressure_complex_from_npz``).
"""

from __future__ import annotations

import argparse
import fcntl
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
# Workspace checkouts win over installed packages when present: the
# hornlab-metal-bem sibling repo, and hornlab-plots/hornlab-sim inside the
# sibling HornLab checkout. Elsewhere the imports resolve from the active
# environment (see requirements.txt).
for package_dir in (
    REPO_ROOT.parent / "hornlab-metal-bem",
    REPO_ROOT.parent / "HornLab" / "hornlab-plots",
    REPO_ROOT.parent / "HornLab" / "hornlab-sim",
):
    if package_dir.is_dir() and str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))

from hornlab_sim.methods import bandpass, driver_coupling, radiation_impedance  # noqa: E402
from hornlab_plots import (  # noqa: E402
    FrequencyResponseCurve,
    save_directivity_plot,
    save_frequency_response_plot,
)
from hornlab_metal_bem import (  # noqa: E402
    ObservationConfig,
    ObservationFrame,
    SolveConfig,
    solve,
    solve_multi_source,
)

# Recorded in manifests: where the solver package actually resolved from.
METAL_BEM_DIR = Path(sys.modules["hornlab_metal_bem"].__file__).resolve().parent


P_REF = 2.0e-5
SPEED_OF_SOUND_M_S = 343.0
CANONICAL_SOLVE_SOURCE_PRIORITY = {
    "HF": 0,
    "MF": 1,
    "LF": 2,
}
DEFAULT_DIRECT_SOLVE_LOCK_PATH = Path("/tmp/hornlab-fusion-direct-solve.lock")


class PressureBasis:
    """In-memory pressure basis; ``pressure_complex`` is engineering convention.

    All loaded bases carry ``e^{+j omega t}`` phasors (a wave arriving tau
    later has phase ``e^{-j omega tau}``), regardless of the on-disk format.
    """

    def __init__(
        self,
        *,
        source_name: str,
        source_tag: int,
        frequencies_hz: np.ndarray,
        observation_angles_deg: np.ndarray,
        observation_planes: np.ndarray,
        pressure_complex: np.ndarray,
    ) -> None:
        self.source_name = source_name
        self.source_tag = source_tag
        self.frequencies_hz = frequencies_hz
        self.observation_angles_deg = observation_angles_deg
        self.observation_planes = observation_planes
        self.pressure_complex = pressure_complex


def _normalize_bem_formulation(raw: str) -> str:
    value = str(raw).strip().lower().replace("-", "_")
    if value not in {"standard", "complex_k"}:
        raise argparse.ArgumentTypeError(
            "BEM formulation must be one of: standard, complex_k, complex-k"
        )
    return value


def _parse_source(raw: str) -> tuple[str, int]:
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) == 2 and parts[0]:
        return parts[0], int(parts[1])
    if len(parts) == 3 and parts[0]:
        return parts[0], int(parts[2])
    raise argparse.ArgumentTypeError(
        "--source expects NAME:TAG or NAME:RES_MM:TAG"
    )


def _split_sources(raw_sources: list[str]) -> list[tuple[str, int]]:
    sources: list[tuple[str, int]] = []
    for raw in raw_sources:
        for part in raw.split(","):
            part = part.strip()
            if part:
                sources.append(_parse_source(part))
    return sources


def _order_sources_for_solves(
    sources: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    def _sort_key(item: tuple[int, tuple[str, int]]) -> tuple[int, int]:
        index, (name, _) = item
        priority = CANONICAL_SOLVE_SOURCE_PRIORITY.get(
            name.strip().upper(),
            len(CANONICAL_SOLVE_SOURCE_PRIORITY),
        )
        return priority, index

    return [source for _, source in sorted(enumerate(sources), key=_sort_key)]


def _parse_source_freq_max(raw_entries: list[str]) -> dict[str, float]:
    limits: dict[str, float] = {}
    for raw in raw_entries:
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) != 2 or not parts[0]:
            raise SystemExit(f"--source-freq-max expects NAME:HZ, got {raw!r}")
        try:
            freq_max_hz = float(parts[1])
        except ValueError as exc:
            raise SystemExit(f"invalid --source-freq-max frequency in {raw!r}") from exc
        if freq_max_hz <= 0.0:
            raise SystemExit(f"--source-freq-max frequency must be positive in {raw!r}")
        limits[parts[0]] = freq_max_hz
    return limits


def _parse_vec3(raw: str, *, name: str) -> np.ndarray:
    aliases = {
        "+x": [1.0, 0.0, 0.0],
        "x": [1.0, 0.0, 0.0],
        "-x": [-1.0, 0.0, 0.0],
        "+y": [0.0, 1.0, 0.0],
        "y": [0.0, 1.0, 0.0],
        "-y": [0.0, -1.0, 0.0],
        "+z": [0.0, 0.0, 1.0],
        "z": [0.0, 0.0, 1.0],
        "-z": [0.0, 0.0, -1.0],
    }
    key = raw.strip().lower()
    if key in aliases:
        return np.asarray(aliases[key], dtype=np.float64)
    try:
        values = [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an axis alias or x,y,z") from exc
    if len(values) != 3:
        raise argparse.ArgumentTypeError(f"{name} must have exactly 3 values")
    return np.asarray(values, dtype=np.float64)


def _unit(vec: np.ndarray, *, name: str) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 1.0e-12:
        raise argparse.ArgumentTypeError(f"{name} must be non-zero")
    return vec / norm


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "source"


def _source_role(source_name: str) -> str:
    role = source_name.strip().lower()
    return role if role in {"lf", "mf", "hf"} else "other"


def _port_exit_apertures(sources: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return [
        (name, tag)
        for name, tag in sources
        if name.strip().upper().startswith("PORT_EXIT")
    ]


def _frequency_grid(
    *,
    freq_min_hz: float,
    freq_max_hz: float,
    freq_count: int,
    freq_spacing: str,
) -> np.ndarray:
    if freq_spacing == "log":
        return np.geomspace(freq_min_hz, freq_max_hz, freq_count)
    return np.linspace(freq_min_hz, freq_max_hz, freq_count)


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return value


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(_jsonable(data), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _direct_solve_lock_path() -> Path:
    return Path(
        os.environ.get(
            "HORNLAB_FUSION_DIRECT_SOLVE_LOCK",
            str(DEFAULT_DIRECT_SOLVE_LOCK_PATH),
        )
    ).expanduser()


def _update_manifest(path: Path, manifest: dict[str, Any], **updates: Any) -> None:
    manifest.update(updates)
    _write_json(path, manifest)


def _acquire_direct_solve_lock(
    manifest_path: Path,
    manifest: dict[str, Any],
):
    lock_path = _direct_solve_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open append-mode so a waiting process does not truncate/overwrite the
    # actual holder's pid record; the pid is written only after acquisition.
    lock_file = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _update_manifest(
            manifest_path,
            manifest,
            status="waiting_for_solve_lock",
            current_phase="waiting_for_existing_direct_solve",
            solve_lock={"path": str(lock_path), "pid": os.getpid()},
        )
        print(
            f"Waiting for existing HornLab direct solve lock: {lock_path}",
            flush=True,
        )
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    lock_file.truncate(0)
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    _update_manifest(
        manifest_path,
        manifest,
        status="running",
        current_phase="direct_solve_lock_acquired",
        solve_lock={"path": str(lock_path), "pid": os.getpid()},
    )
    return lock_file


def _radiation_matrix_freq_max_hz(
    *,
    args: argparse.Namespace,
    apertures: list[tuple[str, int]],
    source_freq_max: dict[str, float],
) -> float:
    return min(
        [float(args.freq_max_hz)]
        + [
            float(source_freq_max[name])
            for name, _tag in apertures
            if name in source_freq_max
        ]
    )


def _radiation_impedance_payload(
    *,
    result: radiation_impedance.RadiationImpedanceResult,
    diagnostics: radiation_impedance.RadiationMatrixDiagnostics,
    npz_path: Path,
    summary_path: Path,
    freq_max_hz: float,
    in_phase_loads: np.ndarray,
    in_phase_names: list[str] | None = None,
) -> dict[str, Any]:
    if in_phase_names is None:
        in_phase_names = list(result.aperture_names)
    return {
        "status": "complete",
        "type": "port_exit_radiation_impedance_matrix",
        "outputs": {
            "npz": str(npz_path),
            "summary_json": str(summary_path),
        },
        "convention": {
            "solver_matrix": (
                "Z_solver maps aperture volume velocity to solver-convention "
                "average pressure"
            ),
            "engineering_matrix": (
                "conj(Z_solver), validated by 260611 termination-attribution "
                "for e^{+j omega t} LEM/TMM insertion"
            ),
            "in_phase_termination_load": (
                "sum_j conj(Z_solver[i,j]) for unit in-phase source weights"
            ),
        },
        "freq_max_hz": float(freq_max_hz),
        "frequencies_hz": result.frequencies_hz,
        "apertures": [
            {
                "name": name,
                "area_m2": float(result.aperture_area_m2[name]),
            }
            for name in result.aperture_names
        ],
        "matrix_shape": list(result.impedance_matrix.shape),
        "diagnostics": {
            "reciprocity_max_abs": diagnostics.reciprocity_max_abs,
            "reciprocity_max_rel": diagnostics.reciprocity_max_rel,
            "reciprocity_max_rel_peak": float(
                np.max(diagnostics.reciprocity_max_rel)
            ),
            "passivity_min_eig": diagnostics.passivity_min_eig,
            "passivity_min_eig_min": float(np.min(diagnostics.passivity_min_eig)),
            "passivity_ok": diagnostics.passivity_ok,
            "passivity_all_ok": bool(np.all(diagnostics.passivity_ok)),
        },
        "in_phase_termination_load": {
            name: in_phase_loads[:, idx]
            for idx, name in enumerate(in_phase_names)
        },
    }


def _result_directivity_payload(result) -> dict[str, list[list[list[float]]]]:
    payload: dict[str, list[list[list[float]]]] = {}
    for plane_index, plane in enumerate(result.observation_planes):
        patterns = []
        for freq_index in range(len(result.frequencies_hz)):
            patterns.append(
                [
                    [float(angle), float(db)]
                    for angle, db in zip(
                        result.observation_angles_deg,
                        result.directivity_db[freq_index, plane_index, :],
                        strict=True,
                    )
                ]
            )
        payload[str(plane)] = patterns
    return payload


def _on_axis_spl_db(result) -> np.ndarray:
    on_axis_idx = int(np.argmin(np.abs(result.observation_angles_deg)))
    pressure = np.asarray(result.pressure_complex[:, 0, on_axis_idx])
    return 20.0 * np.log10(np.maximum(np.abs(pressure), 1.0e-30) / P_REF)


def _result_payload(result, *, source_name: str, source_tag: int) -> dict[str, Any]:
    on_axis = _on_axis_spl_db(result)
    return {
        "source": {"name": source_name, "tag": source_tag},
        "frequencies_hz": result.frequencies_hz,
        "observation_angles_deg": result.observation_angles_deg,
        "observation_planes": result.observation_planes,
        "on_axis_spl_db": on_axis,
        "normalized_spl_db": result.directivity_db,
        "impedance": result.impedance,
        "surface_pressure_avg": result.surface_pressure_avg or {},
        "timings": result.timings,
        "solver_log": result.solver_log,
        "mesh_info": result.mesh_info,
    }


# On-disk convention for every pressure NPZ this script writes. The solver's
# raw e^{-i omega t} output is conjugated once at the write boundary; files
# missing the key predate it and hold raw solver-convention data.
PRESSURE_NPZ_PHASE_CONVENTION = "engineering_exp_plus_jwt"


def _pressure_complex_from_npz(data, *, path: Path) -> np.ndarray:
    """Return the engineering-convention pressure grid from a loaded NPZ."""
    pressure = np.asarray(data["pressure_complex"], dtype=np.complex128)
    if "phase_convention" not in data:
        # Legacy artifact written before the convention key existed: raw
        # solver e^{-i omega t} output.
        return np.conjugate(pressure)
    convention = str(np.asarray(data["phase_convention"]).item())
    if convention != PRESSURE_NPZ_PHASE_CONVENTION:
        raise ValueError(
            f"{path} stores pressure_complex with unsupported phase "
            f"convention {convention!r}; expected "
            f"{PRESSURE_NPZ_PHASE_CONVENTION!r}"
        )
    return pressure


def _write_pressure_basis_npz(path: Path, result, *, source_name: str, source_tag: int) -> None:
    np.savez_compressed(
        path,
        source_name=np.asarray(source_name),
        source_tag=np.asarray(source_tag, dtype=np.int32),
        frequencies_hz=np.asarray(result.frequencies_hz, dtype=np.float64),
        observation_angles_deg=np.asarray(result.observation_angles_deg, dtype=np.float64),
        observation_planes=np.asarray(result.observation_planes, dtype=str),
        pressure_complex=np.conjugate(
            np.asarray(result.pressure_complex, dtype=np.complex128)
        ),
        phase_convention=np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
    )


def _load_pressure_basis(path: Path) -> PressureBasis:
    with np.load(path, allow_pickle=False) as data:
        return PressureBasis(
            source_name=str(data["source_name"].item()),
            source_tag=int(data["source_tag"]),
            frequencies_hz=np.asarray(data["frequencies_hz"], dtype=np.float64),
            observation_angles_deg=np.asarray(
                data["observation_angles_deg"],
                dtype=np.float64,
            ),
            observation_planes=np.asarray(data["observation_planes"], dtype=str),
            pressure_complex=_pressure_complex_from_npz(data, path=path),
        )


def _interp_complex(freqs: np.ndarray, values: np.ndarray, target_hz: float) -> complex:
    mag = np.interp(float(target_hz), freqs, np.abs(values))
    phase = np.interp(float(target_hz), freqs, np.unwrap(np.angle(values)))
    return complex(mag * np.exp(1j * phase))


def _lr4_lowpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return 1.0 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def _lr4_highpass(freqs: np.ndarray, fc_hz: float) -> np.ndarray:
    s = 1j * np.asarray(freqs, dtype=np.float64) / float(fc_hz)
    return (s * s) ** 2 / (s * s + np.sqrt(2.0) * s + 1.0) ** 2


def _spl_db_from_pressure(pressure: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(pressure), 1.0e-30) / P_REF)


def _wrapped_phase_deg(value: complex) -> float:
    return float(np.degrees(np.angle(value)))


def _phase_equivalent_delay_s(phase_diff_rad: float, freq_hz: float) -> float:
    period_s = 1.0 / float(freq_hz)
    delay_s = float(phase_diff_rad) / (2.0 * np.pi * float(freq_hz))
    delay_s = float(delay_s % period_s)
    # An already-aligned pair with tiny negative phase noise wraps to almost
    # exactly one full period; snap that back to zero rather than delaying a
    # full cycle (coherent at the crossover, wrong at every other frequency).
    if period_s - delay_s <= period_s * 1.0e-9:
        return 0.0
    return delay_s


def _crossover_chain(
    present: list[str],
    *,
    lf_mf_hz: float | None,
    mf_hf_hz: float | None,
) -> tuple[list[str], list[float]] | tuple[None, str]:
    """Pick the ordered driver chain and its crossover frequencies.

    Three drivers need both crossover fields. Two drivers form a two-way
    from the pair's natural field first (LF+MF -> LF/MF XO, MF+HF -> MF/HF
    XO), falling back to the single filled field; an LF+HF pair with both
    fields filled is ambiguous. Returns ``(members, crossovers_hz)`` or
    ``(None, reason)``.
    """
    members = [name for name in ("LF", "MF", "HF") if name in present]
    if len(members) < 2:
        return None, "need at least two of LF/MF/HF pressure bases"
    if len(members) == 3:
        if lf_mf_hz is None or mf_hf_hz is None:
            return None, "three-way sum needs both LF/MF and MF/HF crossover frequencies"
        return members, [float(lf_mf_hz), float(mf_hf_hz)]
    natural = {
        ("LF", "MF"): lf_mf_hz,
        ("MF", "HF"): mf_hf_hz,
    }.get((members[0], members[1]))
    if natural is not None:
        return members, [float(natural)]
    provided = [value for value in (lf_mf_hz, mf_hf_hz) if value is not None]
    if len(provided) == 1:
        return members, [float(provided[0])]
    if not provided:
        return None, "no crossover frequency provided"
    return None, (
        f"ambiguous crossover for the {members[0]}/{members[1]} pair: "
        "fill exactly one crossover frequency field"
    )


def _crossover_weights(
    freqs: np.ndarray,
    members: list[str],
    crossovers_hz: list[float],
) -> dict[str, np.ndarray]:
    """LR4 weights along an ordered driver chain: LP, BP..., HP."""
    weights: dict[str, np.ndarray] = {}
    for index, name in enumerate(members):
        weight = np.ones(np.asarray(freqs).shape, dtype=np.complex128)
        if index > 0:
            weight = weight * _lr4_highpass(freqs, crossovers_hz[index - 1])
        if index < len(crossovers_hz):
            weight = weight * _lr4_lowpass(freqs, crossovers_hz[index])
        weights[name] = weight
    return weights


def _member_filter_label(
    name: str,
    index: int,
    members: list[str],
    crossovers_hz: list[float],
    gain_db: float,
) -> str:
    if index == 0:
        filt = f"LR4 LP {crossovers_hz[0]:g} Hz"
    elif index == len(members) - 1:
        filt = f"LR4 HP {crossovers_hz[-1]:g} Hz"
    else:
        filt = f"LR4 BP {crossovers_hz[index - 1]:g}-{crossovers_hz[index]:g} Hz"
    return f"{name} {filt} ({gain_db:+.1f} dB)"


def _level_match_gains_db(
    freqs: np.ndarray,
    filtered_on_axis: dict[str, np.ndarray],
    members: list[str],
    crossovers_hz: list[float],
    solved_top_hz: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, float], float]:
    edges = [float(freqs[0]), *[float(xo) for xo in crossovers_hz], float(freqs[-1])]
    medians: dict[str, float] = {}
    for index, name in enumerate(members):
        band = (freqs >= edges[index]) & (freqs <= edges[index + 1])
        if solved_top_hz and name in solved_top_hz:
            # A clamped source is zero-filled above its solved top; the floored
            # SPL there (~-500 dB) must not drag the in-band median down.
            band = band & (freqs <= solved_top_hz[name] * (1.0 + 1.0e-9))
        spl = _spl_db_from_pressure(filtered_on_axis[name])
        values = spl[band]
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            finite = spl[np.isfinite(spl)]
        medians[name] = float(np.median(finite)) if finite.size else 0.0
    target_db = float(np.median([medians[name] for name in members]))
    gains_db = {name: target_db - medians[name] for name in members}
    return gains_db, medians, target_db


def _interp_pressure_grid(
    freqs_src: np.ndarray,
    pressure: np.ndarray,
    freqs_dst: np.ndarray,
) -> np.ndarray:
    """Interpolate a ``(nf, n_plane, n_angle)`` complex grid onto ``freqs_dst``.

    Magnitude and unwrapped phase are interpolated separately. Frequencies
    above the source grid's top are zeroed rather than extrapolated, so a
    per-source clamped solve contributes nothing beyond its solved band
    (where its crossover filter weight is negligible anyway).
    """
    nf, n_planes, n_angles = pressure.shape
    out = np.zeros((freqs_dst.size, n_planes, n_angles), dtype=np.complex128)
    inside = freqs_dst <= float(freqs_src[-1]) * (1.0 + 1.0e-9)
    if not np.any(inside):
        return out
    for plane in range(n_planes):
        for angle in range(n_angles):
            values = pressure[:, plane, angle]
            mag = np.interp(freqs_dst[inside], freqs_src, np.abs(values))
            phase = np.interp(
                freqs_dst[inside], freqs_src, np.unwrap(np.angle(values))
            )
            out[inside, plane, angle] = mag * np.exp(1j * phase)
    return out


def _harmonize_bases(
    bases: dict[str, PressureBasis],
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, float]]:
    """Bring pressure bases onto one frequency grid for complex summation.

    Angle and plane grids must match exactly. Frequency grids may differ when
    per-source clamping trimmed a solve band; those bases are interpolated
    onto the widest grid and zeroed above their own solved top. Returns
    ``(freqs, pressure_grids, solved_top_hz)``.
    """
    names = list(bases)
    reference = bases[names[0]]
    for name in names[1:]:
        basis = bases[name]
        angles_a = reference.observation_angles_deg
        angles_b = basis.observation_angles_deg
        if angles_a.shape != angles_b.shape or not np.allclose(
            angles_a, angles_b, rtol=1.0e-8, atol=1.0e-10
        ):
            raise ValueError(
                f"pressure basis angle mismatch between "
                f"{reference.source_name} and {basis.source_name}"
            )
        if list(reference.observation_planes) != list(basis.observation_planes):
            raise ValueError(
                f"pressure basis plane mismatch between "
                f"{reference.source_name} and {basis.source_name}"
            )
    master_name = max(names, key=lambda n: float(bases[n].frequencies_hz[-1]))
    master = np.asarray(bases[master_name].frequencies_hz, dtype=np.float64)
    grids: dict[str, np.ndarray] = {}
    solved_top: dict[str, float] = {}
    for name in names:
        basis = bases[name]
        freqs = np.asarray(basis.frequencies_hz, dtype=np.float64)
        solved_top[name] = float(freqs[-1])
        pressure = np.asarray(basis.pressure_complex, dtype=np.complex128)
        if freqs.shape == master.shape and np.allclose(
            freqs, master, rtol=1.0e-8, atol=1.0e-10
        ):
            grids[name] = pressure
        else:
            grids[name] = _interp_pressure_grid(freqs, pressure, master)
    return master, grids, solved_top


def _directivity_payload_from_pressure(
    pressure_complex: np.ndarray,
    *,
    angles_deg: np.ndarray,
    planes: np.ndarray,
) -> dict[str, list[list[list[float]]]]:
    on_axis_idx = int(np.argmin(np.abs(angles_deg)))
    payload: dict[str, list[list[list[float]]]] = {}
    for plane_index, plane in enumerate(planes):
        plane_pressure = np.asarray(pressure_complex[:, plane_index, :])
        reference = np.maximum(
            np.abs(plane_pressure[:, on_axis_idx])[:, None],
            1.0e-30,
        )
        values_db = 20.0 * np.log10(np.maximum(np.abs(plane_pressure), 1.0e-30) / reference)
        payload[str(plane)] = [
            [
                [float(angle), float(db)]
                for angle, db in zip(angles_deg, values_db[freq_index, :], strict=True)
            ]
            for freq_index in range(values_db.shape[0])
        ]
    return payload


def _write_time_alignment_report(
    path: Path,
    *,
    members: list[str],
    crossovers_hz: list[float],
    delays_s: dict[str, float],
    arrival_offsets_s: dict[str, float],
    level_gains_db: dict[str, float],
    level_reference_db: float,
    level_medians_db: dict[str, float],
    phase_rows: list[dict[str, float | str]],
    solved_top_hz: dict[str, float],
    mf_basis_kind: str,
    output_pngs: dict[str, Path],
    alignment_warnings: list[str] | None = None,
) -> None:
    order = sorted(arrival_offsets_s, key=lambda name: arrival_offsets_s[name])
    lines = [
        "Fusion WG Metal driver time-alignment report",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Driver chain: {' -> '.join(members)} ({len(members)}-way)",
        "Crossover settings:",
    ]
    for index, xo in enumerate(crossovers_hz):
        lines.append(f"- {members[index]}/{members[index + 1]}: LR4 at {xo:.3f} Hz")
    lines.extend([
        "",
        "Method:",
        f"- Uses saved complex on-axis BEM pressure bases for {', '.join(members)}.",
        "- Applies LR4 low-pass/band-pass/high-pass filters along the chain.",
        "- Level-matches each channel to the median in-band filtered SPL before summing.",
        "- Chooses the minimum non-negative phase-equivalent delay solution at each crossover frequency.",
        "- Phase is periodic, so these are DSP/acoustic alignment delays, not a unique mechanical path measurement.",
    ])
    if mf_basis_kind != "direct":
        lines.append(f"- MF channel uses the {mf_basis_kind} pressure grid.")
    clamped = {
        name: top
        for name, top in solved_top_hz.items()
        if top < max(solved_top_hz.values()) * (1.0 - 1.0e-9)
    }
    if clamped:
        lines.append(
            "- Clamped solve bands (zero contribution above): "
            + ", ".join(f"{name} {top:.0f} Hz" for name, top in sorted(clamped.items()))
        )
    for warning in alignment_warnings or []:
        lines.append(f"- WARNING: {warning}")
    lines.extend([
        "",
        f"SPL level-match target: {level_reference_db:.2f} dB",
        "Source  In-band median before trim (dB)  Applied gain (dB)",
    ])
    for name in members:
        lines.append(
            f"{name:<6} {level_medians_db[name]:>31.2f} {level_gains_db[name]:>18.2f}"
        )
    lines.extend([
        "",
        "Applied alignment delays:",
        "Source  Added delay (ms)  Implied arrival after first (ms)  Path offset (mm)",
    ])
    max_delay_s = max(delays_s.values())
    for name in members:
        added_ms = delays_s[name] * 1000.0
        arrival_ms = arrival_offsets_s[name] * 1000.0
        path_mm = arrival_offsets_s[name] * SPEED_OF_SOUND_M_S * 1000.0
        marker = " (first)" if abs(delays_s[name] - max_delay_s) <= 1.0e-12 else ""
        lines.append(
            f"{name:<6} {added_ms:>15.3f} {arrival_ms:>31.3f} {path_mm:>16.1f}{marker}"
        )
    lines.extend(["", "Arrival order:"])
    first = order[0]
    for idx, name in enumerate(order, start=1):
        offset_ms = arrival_offsets_s[name] * 1000.0
        path_mm = arrival_offsets_s[name] * SPEED_OF_SOUND_M_S * 1000.0
        suffix = "first" if name == first else f"{offset_ms:.3f} ms after {first} (~{path_mm:.1f} mm)"
        lines.append(f"{idx}. {name}: {suffix}")
    lines.extend(["", "Crossover phase checks:"])
    for row in phase_rows:
        lines.append(
            "- {pair} at {freq_hz:.3f} Hz: raw phase difference {raw_phase_deg:.2f} deg, "
            "aligned phase difference {aligned_phase_deg:.2f} deg, relative delay {relative_delay_ms:.3f} ms".format(
                **row
            )
        )
    lines.extend([
        "",
        "Interference heatmap: 0 dB means the drivers add fully coherently at "
        "that angle/frequency; deep negative values mark driver-spacing "
        "cancellation. Look along the crossover bands, where two drivers "
        "carry comparable level.",
        "",
    ])
    for label, png in output_pngs.items():
        lines.append(f"{label}: {png}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _save_interference_heatmap(
    output_png: Path,
    freqs: np.ndarray,
    aligned_grids: dict[str, np.ndarray],
    *,
    members: list[str],
    crossovers_hz: list[float] | None = None,
    angles_deg: np.ndarray,
    planes: np.ndarray,
    mesh_valid_hz: float | None,
    mesh_valid_radiating_hz: float | None,
) -> Path:
    """Heatmap of the coherent-vs-incoherent sum ratio per angle/frequency.

    ``20*log10(|sum p| / sum |p|)`` is 0 dB where the drivers add fully in
    phase and drops toward the clip floor where driver-spacing path
    differences cancel; the crossover bands, where two drivers carry
    comparable level, are where it matters.
    """
    import matplotlib.pyplot as plt
    from hornlab_plots import prepare_heatmap_data, render_single_heatmap
    from hornlab_plots.style import FIGURE_BG, TEXT_COLOR

    coherent = np.abs(sum(aligned_grids[name] for name in members))
    incoherent = sum(np.abs(aligned_grids[name]) for name in members)
    ratio_db = 20.0 * np.log10(
        np.maximum(coherent, 1.0e-30) / np.maximum(incoherent, 1.0e-30)
    )

    plane_names = [str(plane) for plane in planes]
    fig, axes = plt.subplots(
        len(plane_names), 1, figsize=(11.0, 4.6 * len(plane_names))
    )
    if len(plane_names) == 1:
        axes = [axes]
    fig.patch.set_facecolor(FIGURE_BG)
    for plane_index, (ax, plane) in enumerate(zip(axes, plane_names)):
        values = ratio_db[:, plane_index, :].T  # (n_angle, n_freq)
        angles_p, freqs_p, values_p = prepare_heatmap_data(
            np.asarray(angles_deg, dtype=float),
            np.asarray(freqs, dtype=float),
            values,
        )
        render_single_heatmap(
            ax,
            freqs_p,
            angles_p,
            values_p,
            f"{plane[:1].upper()} Driver Interference (0 dB = coherent sum)",
            reference_level=-6.0,
            mesh_valid_hz=mesh_valid_hz,
            mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        )
        # Point straight at the bands that matter: cancellation lives where
        # two drivers carry comparable level, around each crossover.
        for xo in crossovers_hz or []:
            ax.axvline(
                float(xo),
                color=TEXT_COLOR,
                linestyle=":",
                linewidth=1.2,
                alpha=0.8,
                zorder=4,
            )
            ax.annotate(
                f"XO {xo:g} Hz",
                xy=(float(xo), angles_p[-1]),
                xytext=(3, -12),
                textcoords="offset points",
                color=TEXT_COLOR,
                fontsize=8,
                alpha=0.9,
            )
    fig.tight_layout(pad=1.5)
    out = Path(output_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        str(out),
        format="png",
        dpi=150,
        facecolor=fig.get_facecolor(),
        edgecolor="none",
        bbox_inches="tight",
    )
    plt.close(fig)
    return out


# Roles cycled through the off-axis curves purely for distinct canonical
# colors; 0 deg gets the bold "combined" style.
_OFF_AXIS_TARGET_ANGLES_DEG = (0.0, 15.0, 30.0, 45.0, 60.0)
_OFF_AXIS_CURVE_ROLES = ("combined", "hf", "mf", "lf", "raw", "other")


def _save_off_axis_response_plot(
    output_png: Path,
    freqs: np.ndarray,
    combined_grid: np.ndarray,
    *,
    plane_index: int,
    plane_name: str,
    angles_deg: np.ndarray,
    polar_distance_m: float,
    title: str,
    mesh_valid_hz: float | None,
    mesh_valid_radiating_hz: float | None,
) -> Path | None:
    """Combined SPL at a handful of off-axis angles for one plane.

    On-axis alignment cannot fix off-axis path differences, so crossover
    suck-outs that only exist off axis show up here.
    """
    angles = np.asarray(angles_deg, dtype=float)
    picked: list[int] = []
    for target in _OFF_AXIS_TARGET_ANGLES_DEG:
        index = int(np.argmin(np.abs(angles - target)))
        if index not in picked:
            picked.append(index)
    curves = []
    for order, index in enumerate(picked):
        role = _OFF_AXIS_CURVE_ROLES[min(order, len(_OFF_AXIS_CURVE_ROLES) - 1)]
        curves.append(
            FrequencyResponseCurve(
                frequencies=freqs,
                spl_db=_spl_db_from_pressure(combined_grid[:, plane_index, index]),
                label=f"{angles[index]:g} deg",
                role=role,
            )
        )
    return save_frequency_response_plot(
        output_png,
        curves,
        title=title,
        ylabel=f"{plane_name} SPL at {polar_distance_m:g} m [dB]",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        span_db=58.0,
    )


# VituixCAD reads per-angle FRD files; plane subfolders keep the filename's
# trailing token the angle, which is what its measurement parser keys on.
_VITUIXCAD_PLANE_DIRS = {"horizontal": "hor", "vertical": "ver"}
_VITUIXCAD_DRIVER_NAMES = ("LF", "MF", "HF")


def _write_frd(path: Path, freqs: np.ndarray, pressure: np.ndarray, *, comment: str) -> None:
    spl_db = _spl_db_from_pressure(pressure)
    phase_deg = np.degrees(np.angle(pressure))
    lines = [f"* {comment}"]
    for freq, spl, phase in zip(freqs, spl_db, phase_deg, strict=True):
        lines.append(f"{freq:.6f}\t{spl:.4f}\t{phase:.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _vxp_text(parent: ET.Element, tag: str, value: Any | None = None) -> ET.Element:
    child = ET.SubElement(parent, tag)
    if value is not None:
        child.text = str(value)
    return child


def _vxp_fmt(value: float | int | str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    number = float(value)
    if abs(number) < 5.0e-13:
        number = 0.0
    return f"{number:.12g}"


def _vxp_param(
    parent: ET.Element,
    index: int,
    *,
    name: str,
    value: float | int | str,
    unit: str = "",
    minimum: float | int | str,
    maximum: float | int | str,
) -> None:
    param = ET.SubElement(parent, "PARAM", {"pi": str(index)})
    _vxp_text(param, "Name", name)
    _vxp_text(param, "Value", _vxp_fmt(value))
    _vxp_text(param, "Unit", unit)
    _vxp_text(param, "Optimize", "False")
    _vxp_text(param, "Expression")
    _vxp_text(param, "Min", _vxp_fmt(minimum))
    _vxp_text(param, "Max", _vxp_fmt(maximum))
    _vxp_text(param, "OptiBlock", "False")


def _vxp_wire_points(parent: ET.Element, points: list[tuple[int, int]]) -> None:
    for index, (x, y) in enumerate(points):
        wire = ET.SubElement(parent, "WIRE", {"wi": str(index)})
        _vxp_text(wire, "X", x)
        _vxp_text(wire, "Y", y)


def _vxp_target(parent: ET.Element, tag: str, *, spl: float) -> None:
    target = ET.SubElement(parent, tag)
    _vxp_text(target, "FreqMin", "20.0")
    _vxp_text(target, "FreqMax", "20000.0")
    _vxp_text(target, "SPL", f"{spl:.1f}")
    _vxp_text(target, "Tilt", "0.0")
    _vxp_text(target, "DrvN", 1)
    _vxp_text(target, "Invert", "False")
    _vxp_text(target, "FreeLF", "False")
    _vxp_text(target, "FreeHF", "False")


def _vxp_part(
    parent: ET.Element,
    index: int,
    *,
    part_type: str,
    center_x: int,
    center_y: int,
) -> ET.Element:
    part = ET.SubElement(parent, "PART", {"xi": str(index)})
    _vxp_text(part, "Type", part_type)
    _vxp_text(part, "CenX", center_x)
    _vxp_text(part, "CenY", center_y)
    return part


def _vxp_add_driver_header(
    root: ET.Element,
    *,
    driver_index: int,
    name: str,
    basis: PressureBasis,
    mirror_angles: bool,
) -> None:
    driver = ET.SubElement(root, "DRIVER", {"di": str(driver_index)})
    _vxp_text(driver, "Model", name)
    _vxp_text(driver, "SPL", 80)
    _vxp_text(driver, "Z", 8)
    _vxp_text(driver, "ExtendedData", "False")
    _vxp_text(driver, "ResponseDirectory", ".")
    _vxp_text(driver, "ResponseScale", 1)
    _vxp_text(driver, "ResponseDelay", 0)
    _vxp_text(driver, "ResponseInvert", "False")
    _vxp_text(driver, "ResponseMute", "False")
    _vxp_text(driver, "MinimumPhase", "False")
    _vxp_text(driver, "ResponseSmooth", "None")
    _vxp_text(driver, "ImpedanceFile")
    _vxp_text(driver, "ImpedanceScale", 1)

    response_index = 0
    safe_name = _safe_stem(name)
    angles = np.asarray(basis.observation_angles_deg, dtype=np.float64)
    mirror = mirror_angles and float(np.min(angles)) >= 0.0
    for plane in (str(p) for p in basis.observation_planes):
        plane_dir = _VITUIXCAD_PLANE_DIRS.get(plane, _safe_stem(plane))
        for angle in angles:
            angle_labels = [float(angle)]
            if mirror and 0.0 < abs(float(angle)) < 180.0:
                angle_labels.append(-float(angle))
            for label_angle in angle_labels:
                label = f"{label_angle:g}"
                response = ET.SubElement(driver, "RESPONSE", {"ri": str(response_index)})
                _vxp_text(response, "FileName", f"{plane_dir}/{safe_name} {label}.frd")
                if plane_dir == "ver":
                    _vxp_text(response, "Hor", 0)
                    _vxp_text(response, "Ver", label)
                else:
                    _vxp_text(response, "Hor", label)
                    _vxp_text(response, "Ver", 0)
                response_index += 1


def _vxp_add_driver_part(
    crossover: ET.Element,
    part_index: int,
    *,
    name: str,
    center_x: int,
    center_y: int,
    driver_id: int,
) -> ET.Element:
    part = _vxp_part(
        crossover,
        part_index,
        part_type="Driver",
        center_x=center_x,
        center_y=center_y,
    )
    _vxp_text(part, "Model", name)
    _vxp_text(part, "Open", "False")
    _vxp_text(part, "Shorted", "False")
    _vxp_text(part, "Muted", "False")
    _vxp_text(part, "Hidden", "False")
    _vxp_text(part, "Inverted", "False")
    _vxp_text(part, "PartID", f"D{driver_id}")
    _vxp_text(part, "GUID")
    _vxp_target(part, "DriverTarget", spl=85.0)
    _vxp_target(part, "FilterTarget", spl=0.0)
    _vxp_param(part, 0, name="X", value=0, unit="mm", minimum=-2000, maximum=2000)
    _vxp_param(part, 1, name="Y", value=0, unit="mm", minimum=-5000, maximum=5000)
    _vxp_param(part, 2, name="Z", value=0, unit="mm", minimum=-2000, maximum=2000)
    _vxp_param(part, 3, name="R", value=0, unit="deg", minimum=-180, maximum=180)
    _vxp_param(part, 4, name="T", value=0, unit="deg", minimum=-180, maximum=180)
    _vxp_wire_points(part, [(center_x - 1, center_y - 3), (center_x - 1, center_y + 3)])
    return part


def _vxp_add_active_filter_part(
    crossover: ET.Element,
    part_index: int,
    *,
    kind: str,
    center_x: int,
    signal_y: int,
    frequency_hz: float,
    unit_id: int,
) -> None:
    part_type = "Active High pass" if kind == "hp" else "Active Low pass"
    part = _vxp_part(
        crossover,
        part_index,
        part_type=part_type,
        center_x=center_x,
        center_y=signal_y + 1,
    )
    _vxp_text(part, "Shape", "Linkwitz-Riley")
    _vxp_text(part, "Order", 4)
    _vxp_text(part, "Open", "False")
    _vxp_text(part, "Shorted", "False")
    _vxp_text(part, "PartID", f"U{unit_id}")
    _vxp_text(part, "GUID")
    _vxp_param(
        part,
        0,
        name="f",
        value=frequency_hz,
        unit="Hz",
        minimum=5,
        maximum=40000,
    )
    _vxp_wire_points(
        part,
        [
            (center_x - 3, signal_y),
            (center_x - 3, signal_y + 2),
            (center_x + 3, signal_y),
            (center_x + 3, signal_y + 2),
        ],
    )


def _vxp_add_buffer_part(
    crossover: ET.Element,
    part_index: int,
    *,
    center_x: int,
    signal_y: int,
    gain_db: float,
    delay_ms: float,
    buffer_id: int,
) -> None:
    part = _vxp_part(
        crossover,
        part_index,
        part_type="Buffer",
        center_x=center_x,
        center_y=signal_y + 1,
    )
    _vxp_text(part, "Shape")
    _vxp_text(part, "Open", "False")
    _vxp_text(part, "Shorted", "False")
    _vxp_text(part, "Inverted", "False")
    _vxp_text(part, "PartID", f"A{buffer_id}")
    _vxp_text(part, "GUID")
    _vxp_param(part, 0, name="A", value=gain_db, unit="dB", minimum=-100, maximum=100)
    _vxp_param(
        part,
        1,
        name="dt",
        value=float(delay_ms) * 1000.0,
        unit="us",
        minimum=-50000,
        maximum=50000,
    )
    _vxp_wire_points(
        part,
        [
            (center_x - 3, signal_y),
            (center_x - 3, signal_y + 2),
            (center_x + 3, signal_y),
            (center_x + 3, signal_y + 2),
        ],
    )


def _vxp_add_wire_part(
    crossover: ET.Element,
    part_index: int,
    points: list[tuple[int, int]],
) -> None:
    center_x = int(round(sum(x for x, _y in points) / len(points)))
    center_y = int(round(sum(y for _x, y in points) / len(points)))
    part = _vxp_part(
        crossover,
        part_index,
        part_type="Wire",
        center_x=center_x,
        center_y=center_y,
    )
    _vxp_text(part, "Open", "False")
    _vxp_text(part, "GUID")
    _vxp_wire_points(part, points)


def _vxp_add_ground_part(
    crossover: ET.Element,
    part_index: int,
    *,
    x: int,
    y: int,
) -> None:
    part = _vxp_part(
        crossover,
        part_index,
        part_type="Ground",
        center_x=x,
        center_y=y + 1,
    )
    _vxp_text(part, "Open", "False")
    _vxp_text(part, "Rotated", "False")
    _vxp_text(part, "GUID")
    _vxp_wire_points(part, [(x, y)])


def _vxp_add_project_defaults(root: ET.Element) -> None:
    _vxp_text(root, "Description", "HornLab active LR4 project")
    _vxp_text(root, "ReferenceAngle", 0)
    _vxp_text(root, "DualPlane", "True")
    _vxp_text(root, "KeywordHor", "hor")
    _vxp_text(root, "KeywordVer", "ver")
    _vxp_text(root, "AngleMultiplier", 1)
    _vxp_text(root, "XMin", 20)
    _vxp_text(root, "XMax", 20000)
    _vxp_text(root, "Interpolate", "True")
    _vxp_text(root, "UserAnglesHor")
    _vxp_text(root, "UserAnglesVer")
    _vxp_text(root, "IntensitySphere", "True")
    _vxp_text(root, "IntensityCylinder", "False")
    _vxp_text(root, "IncludeHor", "True")
    _vxp_text(root, "IncludeVer", "True")
    _vxp_text(root, "HalfSpace", "False")
    _vxp_text(root, "Corner", "False")
    _vxp_text(root, "LiswinDI", "True")
    _vxp_text(root, "CTA2034Aweights", "True")
    _vxp_text(root, "AngleStep", 10)
    _vxp_text(root, "FrontWall", "False")
    _vxp_text(root, "FrontWallZ", 1000)
    _vxp_text(root, "LeftWall", "False")
    _vxp_text(root, "LeftWallX", -1000)
    _vxp_text(root, "Ceiling", "False")
    _vxp_text(root, "CeilingY", 1500)
    _vxp_text(root, "Floor", "False")
    _vxp_text(root, "FloorY", -1000)
    _vxp_text(root, "Toein", 25)
    _vxp_text(root, "AbsorpWall", 2)
    _vxp_text(root, "AbsorpCeil", 2)
    _vxp_text(root, "AbsorpFloor", 2)
    _vxp_text(root, "ReferDistance", 2000)
    _vxp_text(root, "PlaneRotation", 0)
    _vxp_text(root, "DrvOffsetX", 0)
    _vxp_text(root, "DrvOffsetY", 0)
    _vxp_target(root, "AxialTarget", spl=85.0)
    _vxp_target(root, "PowerTarget", spl=85.0)


def _active_lr4_filter_chain(
    member_index: int,
    crossovers_hz: list[float],
) -> list[tuple[str, float]]:
    chain: list[tuple[str, float]] = []
    if member_index > 0:
        chain.append(("hp", float(crossovers_hz[member_index - 1])))
    if member_index < len(crossovers_hz):
        chain.append(("lp", float(crossovers_hz[member_index])))
    return chain


def _write_vituixcad_active_lr4_vxp(
    export_dir: Path,
    export_bases: list[tuple[str, PressureBasis]],
    crossover_payload: dict[str, Any] | None,
    *,
    mirror_angles: bool,
) -> Path | None:
    if not isinstance(crossover_payload, dict):
        return None
    if crossover_payload.get("status") != "complete":
        return None
    members = [str(name) for name in crossover_payload.get("members", [])]
    crossovers_hz = [
        float(value) for value in crossover_payload.get("crossovers_hz", [])
    ]
    if len(members) < 2 or len(crossovers_hz) != len(members) - 1:
        return None

    bases_by_name = {name: basis for name, basis in export_bases}
    mf_basis = str(crossover_payload.get("mf_basis") or "direct")
    mf_replacement = "MF"
    if mf_basis != "direct" and "MF_cardioid" in bases_by_name:
        mf_replacement = "MF_cardioid"
    schematic_members = [
        mf_replacement if name == "MF" and mf_replacement in bases_by_name else name
        for name in members
    ]
    if any(name not in bases_by_name for name in schematic_members):
        return None

    level_match = crossover_payload.get("level_match", {})
    gains_db = level_match.get("gains_db", {}) if isinstance(level_match, dict) else {}
    delays_ms = crossover_payload.get("delays_ms", {})
    if not isinstance(gains_db, dict) or not isinstance(delays_ms, dict):
        return None

    root = ET.Element("SPEAKER")
    _vxp_add_project_defaults(root)
    for driver_index, name in enumerate(schematic_members):
        _vxp_add_driver_header(
            root,
            driver_index=driver_index,
            name=name,
            basis=bases_by_name[name],
            mirror_angles=mirror_angles,
        )
    _vxp_text(root, "Variant", 0)

    crossover = ET.SubElement(root, "CROSSOVER")
    _vxp_text(crossover, "DSP", "Analog")
    _vxp_text(crossover, "SampleRate", 96000)
    _vxp_text(crossover, "DSPSettings")
    _vxp_text(crossover, "DSPTemplate")

    part_index = 0
    generator = _vxp_part(
        crossover,
        part_index,
        part_type="Generator",
        center_x=3,
        center_y=9,
    )
    part_index += 1
    _vxp_text(generator, "PartID", "G1")
    _vxp_text(generator, "GUID")
    _vxp_param(
        generator,
        0,
        name="Eg",
        value=2.83,
        unit="V",
        minimum=0.01,
        maximum=400,
    )
    _vxp_param(
        generator,
        1,
        name="Tg",
        value=0,
        unit="us",
        minimum=-50000,
        maximum=50000,
    )
    _vxp_param(
        generator,
        2,
        name="Rg",
        value=0.001,
        unit="Ω",
        minimum=0.001,
        maximum=1000,
    )
    _vxp_wire_points(generator, [(3, 6), (3, 12)])

    _vxp_add_ground_part(crossover, part_index, x=3, y=12)
    part_index += 1

    unit_id = 1
    buffer_id = 1
    display_rows = list(
        reversed(list(enumerate(zip(members, schematic_members, strict=True))))
    )
    for row_index, (member_index, (member_name, schematic_name)) in enumerate(display_rows):
        signal_y = 6 + row_index * 14
        current_x = 10
        if signal_y == 6:
            generator_wire = [(3, 6), (current_x, signal_y)]
        else:
            generator_wire = [(3, 6), (6, 6), (6, signal_y), (current_x, signal_y)]
        _vxp_add_wire_part(crossover, part_index, generator_wire)
        part_index += 1

        for kind, frequency_hz in _active_lr4_filter_chain(member_index, crossovers_hz):
            _vxp_add_active_filter_part(
                crossover,
                part_index,
                kind=kind,
                center_x=current_x + 3,
                signal_y=signal_y,
                frequency_hz=frequency_hz,
                unit_id=unit_id,
            )
            part_index += 1
            unit_id += 1
            current_x += 6

        gain_db = float(gains_db.get(member_name, gains_db.get(schematic_name, 0.0)))
        delay_ms = float(
            delays_ms.get(member_name, delays_ms.get(schematic_name, 0.0))
        )
        _vxp_add_buffer_part(
            crossover,
            part_index,
            center_x=current_x + 3,
            signal_y=signal_y,
            gain_db=gain_db,
            delay_ms=delay_ms,
            buffer_id=buffer_id,
        )
        part_index += 1
        buffer_id += 1
        current_x += 6

        driver_pin_x = current_x + 8
        _vxp_add_wire_part(
            crossover,
            part_index,
            [(current_x, signal_y), (driver_pin_x, signal_y)],
        )
        part_index += 1
        _vxp_add_driver_part(
            crossover,
            part_index,
            name=schematic_name,
            center_x=driver_pin_x + 1,
            center_y=signal_y + 3,
            driver_id=member_index + 1,
        )
        part_index += 1
        _vxp_add_ground_part(crossover, part_index, x=driver_pin_x, y=signal_y + 6)
        part_index += 1

    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    path = export_dir / "HornLab_active_lr4.vxp"
    path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="utf-8"?>',
                "<!--VituixCAD PROJECT-->",
                "<!--Version 2-->",
                xml,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_vituixcad_export(
    out_dir: Path,
    source_results: list[dict[str, Any]],
    *,
    polar_distance_m: float,
    passive_payload: dict[str, Any] | None = None,
    active_crossover_payload: dict[str, Any] | None = None,
    mirror_angles: bool = True,
) -> dict[str, Any] | None:
    """Export per-driver per-angle FRD sets for VituixCAD.

    All drivers are solved on one mesh with one observation grid and one
    phase reference, so the exported phase already contains every
    inter-driver path/delay difference — in VituixCAD the drivers' X/Y/Z
    offsets stay 0, exactly like a measurement session with a fixed mic and
    a shared timing reference. The common time-of-flight to the observation
    distance is removed equally from every file (cosmetic: less phase wrap,
    no relative change). Levels are unit-cone-velocity SPL except for a
    coupled MF_cardioid export, which is voltage-driven. No ZMA is exported
    for direct BEM drivers; coupled MF_cardioid carries its calculated ZMA.
    """
    by_name = {str(result["name"]).strip().upper(): result for result in source_results}
    export_bases: list[tuple[str, PressureBasis]] = [
        (name, _load_pressure_basis(Path(by_name[name]["pressure_basis_npz"])))
        for name in _VITUIXCAD_DRIVER_NAMES
        if name in by_name
    ]
    passive_result = _preferred_passive_cardioid_results(passive_payload)
    if passive_result is not None:
        results_npz, _passive_kind = passive_result
        with np.load(results_npz, allow_pickle=False) as data:
            export_bases.append(
                (
                    "MF_cardioid",
                    PressureBasis(
                        source_name="MF_cardioid",
                        source_tag=0,
                        frequencies_hz=np.asarray(
                            data["frequencies_hz"], dtype=np.float64
                        ),
                        observation_angles_deg=np.asarray(
                            data["observation_angles_deg"], dtype=np.float64
                        ),
                        observation_planes=np.asarray(
                            data["observation_planes"], dtype=str
                        ),
                        pressure_complex=_pressure_complex_from_npz(
                            data, path=results_npz
                        ),
                    ),
                )
            )
    if not export_bases:
        return None

    export_dir = out_dir / "vituixcad"
    copied_zma: Path | None = None
    if passive_payload is not None:
        coupled = passive_payload.get("coupled")
        if isinstance(coupled, dict) and coupled.get("status") == "complete":
            outputs = coupled.get("outputs", {})
            if isinstance(outputs, dict) and outputs.get("impedance_zma"):
                source_zma = Path(outputs["impedance_zma"])
                if source_zma.exists():
                    export_dir.mkdir(parents=True, exist_ok=True)
                    copied_zma = export_dir / source_zma.name
                    shutil.copy2(source_zma, copied_zma)
    files_written = 0
    for name, basis in export_bases:
        freqs = np.asarray(basis.frequencies_hz, dtype=np.float64)
        angles = np.asarray(basis.observation_angles_deg, dtype=np.float64)
        # Remove the shared time of flight to the observation distance so
        # the phase traces wrap less; identical for every driver and angle.
        # Engineering-convention bases carry e^{-jkr}, so multiplying by
        # e^{+jk d} cancels the propagation phase.
        tof_phase = np.exp(
            1j * 2.0 * np.pi * freqs * (polar_distance_m / SPEED_OF_SOUND_M_S)
        )
        mirror = mirror_angles and float(np.min(angles)) >= 0.0
        for plane_index, plane in enumerate(str(p) for p in basis.observation_planes):
            plane_dir = export_dir / _VITUIXCAD_PLANE_DIRS.get(plane, _safe_stem(plane))
            plane_dir.mkdir(parents=True, exist_ok=True)
            for angle_index, angle in enumerate(angles):
                pressure = (
                    basis.pressure_complex[:, plane_index, angle_index] * tof_phase
                )
                angle_labels = [f"{angle:g}"]
                if mirror and 0.0 < abs(angle) < 180.0:
                    angle_labels.append(f"{-angle:g}")
                for label in angle_labels:
                    _write_frd(
                        plane_dir / f"{_safe_stem(name)} {label}.frd",
                        freqs,
                        pressure,
                        comment=(
                            f"{name} {plane} {label} deg - HornLab WG Metal BEM, "
                            f"unit cone velocity, common ToF {polar_distance_m:g} m "
                            "removed, shared timing reference (set X/Y/Z=0)"
                        ),
                    )
                    files_written += 1

    active_vxp = _write_vituixcad_active_lr4_vxp(
        export_dir,
        export_bases,
        active_crossover_payload,
        mirror_angles=mirror_angles,
    )
    readme = export_dir / "README.txt"
    zma_lines = (
        [
            "- MF_cardioid includes MF_passive_cardioid_impedance.zma,",
            "  calculated from the coupled driver/LEM/BEM model.",
            "- For non-cardioid drivers import measured or datasheet",
            "  impedance.",
        ]
        if copied_zma is not None
        else [
            "- No ZMA is exported: BEM has no electrical side. For passive",
            "  crossover work import measured or datasheet impedance per",
            "  driver.",
        ]
    )
    readme.write_text(
        "\n".join(
            [
                "VituixCAD export from the HornLab WG Metal BEM pipeline",
                "",
                "Folders: hor/ = horizontal plane, ver/ = vertical plane.",
                "Files: '<driver> <angle>.frd' with 'freq_Hz  SPL_dB  phase_deg'.",
                "",
                "How to use:",
                "- In VituixCAD's Drivers tab load each driver's full angle set",
                "  (hor into the axial/horizontal slots, ver into vertical).",
                "- Set every driver's X/Y/Z offset and delay to 0: all drivers",
                "  share one mesh, one mic grid, and one timing reference, so",
                "  the inter-driver path/phase differences are already in the",
                "  exported phase (like a measurement session with a fixed mic",
                "  and shared timing).",
                "- The common time of flight to the observation distance was",
                "  removed identically from every file; relative data is",
                "  untouched.",
                "- Levels are unit-cone-velocity SPL, not per-volt sensitivity.",
                "  Scale each driver's level to taste (or to measured",
                "  sensitivity) before reading absolute SPL off the charts.",
                *zma_lines,
                "- 'MF_cardioid' (when present) is the passive-cardioid",
                "  combined MF+port response; treat it as the MF driver.",
                (
                    "- HornLab_active_lr4.vxp contains the computed active "
                    "LR4 crossover, gains, and delays."
                    if active_vxp is not None
                    else "- No active .vxp project was written because no "
                    "complete crossover alignment was available."
                ),
                "- For crossover design increase 'Number of frequencies' in the",
                "  add-in (e.g. 120-200) for a denser grid; mind the mesh-valid",
                "  frequency limits recorded in the run manifest.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "status": "complete",
        "type": "vituixcad_frd_export",
        "export_dir": str(export_dir),
        "drivers": [name for name, _ in export_bases],
        "files_written": files_written,
        "mirrored_negative_angles": bool(mirror_angles),
        "outputs": {
            "vituixcad_export_dir": str(export_dir),
            "vituixcad_readme_txt": str(readme),
        },
    }
    if copied_zma is not None:
        payload["outputs"]["vituixcad_mf_cardioid_zma"] = str(copied_zma)
    if active_vxp is not None:
        payload["active_crossover_project"] = {
            "type": "active_lr4_vxp",
            "path": str(active_vxp),
        }
        payload["outputs"]["vituixcad_active_lr4_vxp"] = str(active_vxp)
    return payload


def _write_crossover_alignment_outputs(
    out_dir: Path,
    source_results: list[dict[str, Any]],
    *,
    lf_mf_hz: float | None,
    mf_hf_hz: float | None,
    polar_distance_m: float,
    mesh_valid_hz: float | None,
    mesh_valid_radiating_hz: float | None,
    mf_override_npz: Path | None = None,
    mf_override_kind: str = "direct",
) -> dict[str, Any] | None:
    if lf_mf_hz is None and mf_hf_hz is None:
        return None
    by_name = {str(result["name"]).strip().upper(): result for result in source_results}
    chain, chain_or_reason = _crossover_chain(
        [name for name in ("LF", "MF", "HF") if name in by_name],
        lf_mf_hz=lf_mf_hz,
        mf_hf_hz=mf_hf_hz,
    )
    if chain is None:
        return {"status": "skipped", "reason": str(chain_or_reason)}
    members, crossovers_hz = chain, list(chain_or_reason)

    bases = {
        name: _load_pressure_basis(Path(by_name[name]["pressure_basis_npz"]))
        for name in members
    }
    mf_basis_kind = "direct"
    if mf_override_npz is not None and "MF" in bases:
        with np.load(mf_override_npz, allow_pickle=False) as data:
            bases["MF"] = PressureBasis(
                source_name="MF",
                source_tag=bases["MF"].source_tag,
                frequencies_hz=np.asarray(data["frequencies_hz"], dtype=np.float64),
                observation_angles_deg=np.asarray(
                    data["observation_angles_deg"], dtype=np.float64
                ),
                observation_planes=np.asarray(
                    data["observation_planes"], dtype=str
                ),
                pressure_complex=_pressure_complex_from_npz(
                    data, path=mf_override_npz
                ),
            )
        mf_basis_kind = mf_override_kind
    try:
        freqs, grids, solved_top_hz = _harmonize_bases(bases)
    except ValueError as exc:
        return {
            "status": "skipped",
            "reason": f"pressure grids are not summable: {exc}",
        }

    angles_deg = bases[members[0]].observation_angles_deg
    planes = bases[members[0]].observation_planes
    on_axis_idx = int(np.argmin(np.abs(angles_deg)))
    pressures = {name: grids[name][:, 0, on_axis_idx] for name in members}
    weights = _crossover_weights(freqs, members, crossovers_hz)
    filtered = {name: pressures[name] * weights[name] for name in members}
    level_gains_db, level_medians_db, level_reference_db = _level_match_gains_db(
        freqs, filtered, members, crossovers_hz, solved_top_hz
    )
    level_gain_linear = {
        name: 10.0 ** (level_gains_db[name] / 20.0) for name in members
    }

    # Alignment delays accumulate down the chain from the top driver: each
    # crossover contributes the minimum non-negative phase-equivalent delay
    # between its adjacent pair. A crossover above a clamped member's solved
    # top would read the zero-filled region (phase 0 -> bogus delay), so the
    # pair phase is measured at the highest frequency both members solved.
    alignment_warnings: list[str] = []
    pair_eval_hz: dict[str, float] = {}
    delays_s = {members[-1]: 0.0}
    for index in range(len(members) - 2, -1, -1):
        lower, upper = members[index], members[index + 1]
        xo = crossovers_hz[index]
        limit_hz = min(solved_top_hz[lower], solved_top_hz[upper])
        if limit_hz < xo:
            # Snap to the highest master-grid sample inside both solved
            # bands: interpolating at the boundary itself would blend the
            # zero-filled region's bogus phase into the measurement.
            inside = freqs[freqs <= limit_hz * (1.0 + 1.0e-9)]
            eval_hz = float(inside[-1]) if inside.size else float(limit_hz)
            alignment_warnings.append(
                f"{lower}/{upper} crossover {xo:g} Hz is above the clamped "
                f"solve band; alignment measured at {eval_hz:.0f} Hz instead."
            )
        else:
            eval_hz = float(xo)
        pair_eval_hz[f"{lower}-{upper}"] = eval_hz
        lower_at_xo = _interp_complex(freqs, filtered[lower], eval_hz)
        upper_at_xo = _interp_complex(freqs, filtered[upper], eval_hz)
        pair_delay = _phase_equivalent_delay_s(
            np.angle(lower_at_xo / upper_at_xo), eval_hz
        )
        delays_s[lower] = delays_s[upper] + pair_delay
    min_delay = min(delays_s.values())
    delays_s = {name: value - min_delay for name, value in delays_s.items()}

    # Engineering-convention bases: delaying by tau is e^{-j omega tau}.
    delay_phase = {
        name: np.exp(-1j * 2.0 * np.pi * freqs * delays_s[name]) for name in members
    }
    filtered_level = {
        name: filtered[name] * level_gain_linear[name] for name in members
    }
    aligned = {
        name: filtered_level[name] * delay_phase[name] for name in members
    }
    combined_unaligned = sum(filtered_level[name] for name in members)
    combined_aligned = sum(aligned[name] for name in members)

    xo_text = ", ".join(
        f"{members[index]}/{members[index + 1]} {xo:g} Hz"
        for index, xo in enumerate(crossovers_hz)
    )
    output_png = out_dir / "combined_frequency_response_time_aligned.png"
    member_roles = {"LF": "lf", "MF": "mf", "HF": "hf"}
    curves = [
        FrequencyResponseCurve(
            frequencies=freqs,
            spl_db=_spl_db_from_pressure(aligned[name]),
            label=_member_filter_label(
                name, index, members, crossovers_hz, level_gains_db[name]
            ),
            role=member_roles.get(name, "other"),
            crossover=True,
        )
        for index, name in enumerate(members)
    ]
    curves.extend([
        FrequencyResponseCurve(
            frequencies=freqs,
            spl_db=_spl_db_from_pressure(combined_unaligned),
            label="Combined before delay",
            role="raw",
        ),
        FrequencyResponseCurve(
            frequencies=freqs,
            spl_db=_spl_db_from_pressure(combined_aligned),
            label="Combined time aligned",
            role="combined",
        ),
    ])
    save_frequency_response_plot(
        output_png,
        curves,
        title=f"Level-Matched Time-Aligned LR4 Sum ({xo_text})",
        ylabel=f"On-axis SPL at {polar_distance_m:g} m [dB]",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        span_db=58.0,
    )

    aligned_grids = {
        name: (
            grids[name]
            * (weights[name] * level_gain_linear[name] * delay_phase[name])[
                :, None, None
            ]
        )
        for name in members
    }
    combined_pressure_grid = sum(aligned_grids[name] for name in members)
    heatmap_png = out_dir / "combined_directivity_heatmap_time_aligned.png"
    save_directivity_plot(
        heatmap_png,
        freqs,
        _directivity_payload_from_pressure(
            combined_pressure_grid,
            angles_deg=angles_deg,
            planes=planes,
        ),
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )

    interference_png = out_dir / "combined_interference_heatmap_time_aligned.png"
    _save_interference_heatmap(
        interference_png,
        freqs,
        aligned_grids,
        members=members,
        crossovers_hz=crossovers_hz,
        angles_deg=angles_deg,
        planes=planes,
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )

    off_axis_pngs: dict[str, str] = {}
    for plane_index, plane in enumerate(str(p) for p in planes):
        plane_png = out_dir / (
            f"combined_frequency_response_off_axis_{_safe_stem(plane)}.png"
        )
        saved = _save_off_axis_response_plot(
            plane_png,
            freqs,
            combined_pressure_grid,
            plane_index=plane_index,
            plane_name=plane.capitalize(),
            angles_deg=angles_deg,
            polar_distance_m=polar_distance_m,
            title=f"Time-Aligned Sum Off Axis, {plane.capitalize()} ({xo_text})",
            mesh_valid_hz=mesh_valid_hz,
            mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        )
        if saved is not None:
            off_axis_pngs[plane] = str(plane_png)

    phase_rows = []
    for index, xo in enumerate(crossovers_hz):
        left, right = members[index], members[index + 1]
        eval_hz = pair_eval_hz.get(f"{left}-{right}", float(xo))
        raw_left = _interp_complex(freqs, filtered[left], eval_hz)
        raw_right = _interp_complex(freqs, filtered[right], eval_hz)
        aligned_left = _interp_complex(freqs, aligned[left], eval_hz)
        aligned_right = _interp_complex(freqs, aligned[right], eval_hz)
        phase_rows.append(
            {
                "pair": f"{left}-{right}",
                "freq_hz": float(eval_hz),
                "raw_phase_deg": _wrapped_phase_deg(raw_left / raw_right),
                "aligned_phase_deg": _wrapped_phase_deg(aligned_left / aligned_right),
                "relative_delay_ms": (delays_s[left] - delays_s[right]) * 1000.0,
            }
        )

    max_delay_s = max(delays_s.values())
    arrival_offsets_s = {
        name: max_delay_s - delay_s for name, delay_s in delays_s.items()
    }
    report_txt = out_dir / "driver_time_alignment.txt"
    _write_time_alignment_report(
        report_txt,
        members=members,
        crossovers_hz=crossovers_hz,
        delays_s=delays_s,
        arrival_offsets_s=arrival_offsets_s,
        level_gains_db=level_gains_db,
        level_reference_db=level_reference_db,
        level_medians_db=level_medians_db,
        phase_rows=phase_rows,
        solved_top_hz=solved_top_hz,
        mf_basis_kind=mf_basis_kind,
        alignment_warnings=alignment_warnings,
        output_pngs={
            "Level-matched aligned response plot": output_png,
            "Level-matched aligned directivity heatmap": heatmap_png,
            "Driver interference heatmap": interference_png,
            **{
                f"Off-axis response ({plane})": Path(png)
                for plane, png in off_axis_pngs.items()
            },
        },
    )
    return {
        "status": "complete",
        "type": "lr4_time_aligned_on_axis_sum",
        "members": members,
        "crossovers_hz": [float(xo) for xo in crossovers_hz],
        "lf_mf_hz": float(lf_mf_hz) if lf_mf_hz is not None else None,
        "mf_hf_hz": float(mf_hf_hz) if mf_hf_hz is not None else None,
        "mf_basis": mf_basis_kind,
        "source_solved_freq_max_hz": solved_top_hz,
        "alignment_warnings": alignment_warnings,
        "pair_alignment_eval_hz": pair_eval_hz,
        "outputs": {
            "combined_time_aligned_frequency_response_png": str(output_png),
            "combined_time_aligned_directivity_heatmap_png": str(heatmap_png),
            "combined_interference_heatmap_png": str(interference_png),
            "combined_off_axis_frequency_response_pngs": off_axis_pngs,
            "driver_time_alignment_txt": str(report_txt),
        },
        "level_match": {
            "method": "median filtered on-axis SPL in each channel passband",
            "target_db": level_reference_db,
            "in_band_medians_db": level_medians_db,
            "gains_db": level_gains_db,
        },
        "delays_ms": {name: delay_s * 1000.0 for name, delay_s in delays_s.items()},
        "arrival_offsets_ms": {
            name: offset_s * 1000.0 for name, offset_s in arrival_offsets_s.items()
        },
        "phase_checks": phase_rows,
    }


def _assert_matching_basis_grid(a: PressureBasis, b: PressureBasis) -> None:
    if a.pressure_complex.shape != b.pressure_complex.shape:
        raise ValueError(
            f"pressure basis shape mismatch: {a.source_name} "
            f"{a.pressure_complex.shape} vs {b.source_name} {b.pressure_complex.shape}"
        )
    for attr in ("frequencies_hz", "observation_angles_deg"):
        av = getattr(a, attr)
        bv = getattr(b, attr)
        if av.shape != bv.shape or not np.allclose(av, bv, rtol=1.0e-8, atol=1.0e-10):
            raise ValueError(
                f"pressure basis {attr} mismatch between "
                f"{a.source_name} and {b.source_name}"
            )
    if list(a.observation_planes) != list(b.observation_planes):
        raise ValueError(
            f"pressure basis plane mismatch between {a.source_name} and {b.source_name}"
        )


def _directivity_from_pressure_array(pressure: np.ndarray, angles_deg: np.ndarray) -> np.ndarray:
    on_axis_idx = int(np.argmin(np.abs(angles_deg)))
    floor_amplitude = P_REF * 10.0 ** (-120.0 / 20.0)
    amplitudes = np.maximum(np.abs(pressure), floor_amplitude)
    spl_raw = 20.0 * np.log10(amplitudes / P_REF)
    return spl_raw - spl_raw[:, :, on_axis_idx][:, :, None]


def _directivity_payload_from_arrays(
    angles_deg: np.ndarray,
    planes: np.ndarray,
    directivity_db: np.ndarray,
) -> dict[str, list[list[list[float]]]]:
    payload: dict[str, list[list[list[float]]]] = {}
    for plane_index, plane in enumerate(planes):
        patterns = []
        for freq_index in range(directivity_db.shape[0]):
            patterns.append(
                [
                    [float(angle), float(db)]
                    for angle, db in zip(
                        angles_deg,
                        directivity_db[freq_index, plane_index, :],
                        strict=True,
                    )
                ]
            )
        payload[str(plane)] = patterns
    return payload


def _mesh_tag_area_m2(mesh_path: Path, tag: int, *, mesh_scale: float) -> float:
    try:
        import meshio  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("meshio is required to compute source patch areas") from exc

    mesh = meshio.read(mesh_path)
    tri_key = "triangle" if "triangle" in mesh.cells_dict else "triangle3"
    if tri_key not in mesh.cells_dict:
        raise ValueError(f"mesh has no triangle cells: {mesh_path}")
    triangles = np.asarray(mesh.cells_dict[tri_key], dtype=np.int64)
    points = np.asarray(mesh.points, dtype=np.float64) * float(mesh_scale)
    physical = None
    for key, by_type in mesh.cell_data_dict.items():
        if "physical" in key and tri_key in by_type:
            physical = np.asarray(by_type[tri_key], dtype=np.int32)
            break
    if physical is None:
        raise ValueError(f"mesh has no triangle physical tags: {mesh_path}")
    mask = physical == int(tag)
    if not np.any(mask):
        raise ValueError(f"physical tag {tag} is absent from mesh {mesh_path}")
    tri = triangles[mask]
    p0 = points[tri[:, 0]]
    p1 = points[tri[:, 1]]
    p2 = points[tri[:, 2]]
    areas = 0.5 * np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)
    area = float(np.sum(areas))
    if area <= 0.0 or not np.isfinite(area):
        raise ValueError(f"physical tag {tag} has invalid area {area}")
    return area


def _load_port_exit_termination(
    matrix_npz: Path,
    *,
    port_source_name: str,
) -> tuple[np.ndarray, np.ndarray, float]:
    with np.load(matrix_npz, allow_pickle=False) as data:
        names = [str(name) for name in data["aperture_names"]]
        if port_source_name not in names:
            raise ValueError(
                f"port source {port_source_name!r} not present in "
                f"{matrix_npz}; available apertures: {names}"
            )
        idx = names.index(port_source_name)
        freqs = np.asarray(data["frequencies_hz"], dtype=np.float64)
        solver_matrix = np.asarray(
            data["solver_impedance_matrix"],
            dtype=np.complex128,
        )
        areas = np.asarray(data["aperture_area_m2"], dtype=np.float64)
    termination_load = radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix,
        receiver_index=idx,
    )
    return freqs, termination_load, float(areas[idx])


def _load_port_mf_mutual_impedance(
    matrix_npz: Path,
    *,
    port_source_name: str,
    mf_source_name: str,
) -> np.ndarray | None:
    """Engineering-convention Z(port <- MF) from the aperture matrix.

    Returns None when the matrix predates the MF-aperture extension (the
    combine then falls back to interior drive only).
    """
    with np.load(matrix_npz, allow_pickle=False) as data:
        names = [str(name) for name in data["aperture_names"]]
        if port_source_name not in names or mf_source_name not in names:
            return None
        solver_matrix = np.asarray(
            data["solver_impedance_matrix"],
            dtype=np.complex128,
        )
    return radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix,
        receiver_index=names.index(port_source_name),
        source_indices=[names.index(mf_source_name)],
    )


def _port_group_indices_from_matrix(
    data: Any,
    names: list[str],
    *,
    port_source_name: str,
) -> list[int]:
    if port_source_name not in names:
        raise ValueError(
            f"port source {port_source_name!r} not present in matrix; "
            f"available apertures: {names}"
        )
    port_names = [port_source_name]
    if "in_phase_aperture_names" in data.files:
        in_phase_names = [str(name) for name in data["in_phase_aperture_names"]]
        if port_source_name in in_phase_names:
            port_names = in_phase_names
    return [names.index(name) for name in port_names if name in names]


def _load_mf_self_and_port_mutual(
    matrix_npz: Path,
    *,
    mf_source_name: str,
    port_source_name: str,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Engineering Z(MF <- MF) and Z(MF <- in-phase port group).

    Returns None when the matrix predates the passive-cardioid MF aperture
    extension. The conversion to engineering convention is delegated to
    ``termination_load_from_solver_matrix``.
    """
    with np.load(matrix_npz, allow_pickle=False) as data:
        names = [str(name) for name in data["aperture_names"]]
        if mf_source_name not in names:
            return None
        port_indices = _port_group_indices_from_matrix(
            data,
            names,
            port_source_name=port_source_name,
        )
        solver_matrix = np.asarray(
            data["solver_impedance_matrix"],
            dtype=np.complex128,
        )
    mf_idx = names.index(mf_source_name)
    z_mm = radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix,
        receiver_index=mf_idx,
        source_indices=[mf_idx],
    )
    z_mf_from_port = radiation_impedance.termination_load_from_solver_matrix(
        solver_matrix,
        receiver_index=mf_idx,
        source_indices=port_indices,
    )
    return z_mm, z_mf_from_port


def _source_result_by_name(source_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(result["name"]): result for result in source_results}


def _auto_port_source_name(source_results: list[dict[str, Any]]) -> str | None:
    names = [str(result["name"]) for result in source_results]
    if "PORT_EXIT" in names:
        return "PORT_EXIT"
    for name in names:
        if name.upper().startswith("PORT_EXIT"):
            return name
    return None


def _parse_optional_positive_m2_from_cm2(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0.0 or not np.isfinite(value):
        raise SystemExit("--passive-cardioid-port-area-cm2 must be positive")
    return float(value) * 1.0e-4


def _require_positive_float(args: argparse.Namespace, attr: str, flag: str) -> None:
    value = getattr(args, attr)
    if value is None:
        raise SystemExit(f"{flag} is required with --passive-cardioid-coupled")
    if value <= 0.0 or not np.isfinite(value):
        raise SystemExit(f"{flag} must be positive")


def _validate_optional_positive_float(
    args: argparse.Namespace,
    attr: str,
    flag: str,
) -> None:
    value = getattr(args, attr)
    if value is not None and (value <= 0.0 or not np.isfinite(value)):
        raise SystemExit(f"{flag} must be positive")


def _validate_optional_nonnegative_float(
    args: argparse.Namespace,
    attr: str,
    flag: str,
) -> None:
    value = getattr(args, attr)
    if value is not None and (value < 0.0 or not np.isfinite(value)):
        raise SystemExit(f"{flag} must be non-negative")


def _validate_passive_cardioid_coupled_args(args: argparse.Namespace) -> None:
    for attr, flag in (
        ("passive_cardioid_driver_sd_cm2", "--passive-cardioid-driver-sd-cm2"),
        ("passive_cardioid_driver_bl_tm", "--passive-cardioid-driver-bl-tm"),
        ("passive_cardioid_driver_re_ohm", "--passive-cardioid-driver-re-ohm"),
    ):
        _require_positive_float(args, attr, flag)
    _validate_optional_nonnegative_float(
        args,
        "passive_cardioid_driver_le_mh",
        "--passive-cardioid-driver-le-mh",
    )
    if (
        (args.passive_cardioid_driver_le2_mh is None)
        != (args.passive_cardioid_driver_re2_ohm is None)
    ):
        raise SystemExit(
            "--passive-cardioid-driver-le2-mh and "
            "--passive-cardioid-driver-re2-ohm must be provided together"
        )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_le2_mh",
        "--passive-cardioid-driver-le2-mh",
    )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_re2_ohm",
        "--passive-cardioid-driver-re2-ohm",
    )
    mass_count = sum(
        value is not None
        for value in (
            args.passive_cardioid_driver_mmd_g,
            args.passive_cardioid_driver_mms_g,
        )
    )
    if mass_count != 1:
        raise SystemExit(
            "exactly one of --passive-cardioid-driver-mmd-g or "
            "--passive-cardioid-driver-mms-g is required"
        )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_mmd_g",
        "--passive-cardioid-driver-mmd-g",
    )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_mms_g",
        "--passive-cardioid-driver-mms-g",
    )
    compliance_count = sum(
        value is not None
        for value in (
            args.passive_cardioid_driver_cms_mm_per_n,
            args.passive_cardioid_driver_vas_l,
            args.passive_cardioid_driver_fs_hz,
        )
    )
    if compliance_count != 1:
        raise SystemExit(
            "exactly one of --passive-cardioid-driver-cms-mm-per-n, "
            "--passive-cardioid-driver-vas-l, or "
            "--passive-cardioid-driver-fs-hz is required"
        )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_cms_mm_per_n",
        "--passive-cardioid-driver-cms-mm-per-n",
    )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_vas_l",
        "--passive-cardioid-driver-vas-l",
    )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_fs_hz",
        "--passive-cardioid-driver-fs-hz",
    )
    if (
        args.passive_cardioid_driver_rms_kg_per_s is not None
        and args.passive_cardioid_driver_qms is not None
    ):
        raise SystemExit(
            "--passive-cardioid-driver-rms-kg-per-s and "
            "--passive-cardioid-driver-qms are mutually exclusive"
        )
    _validate_optional_nonnegative_float(
        args,
        "passive_cardioid_driver_rms_kg_per_s",
        "--passive-cardioid-driver-rms-kg-per-s",
    )
    _validate_optional_positive_float(
        args,
        "passive_cardioid_driver_qms",
        "--passive-cardioid-driver-qms",
    )
    if args.passive_cardioid_driver_count <= 0:
        raise SystemExit("--passive-cardioid-driver-count must be positive")
    if (
        args.passive_cardioid_drive_voltage <= 0.0
        or not np.isfinite(args.passive_cardioid_drive_voltage)
    ):
        raise SystemExit("--passive-cardioid-drive-voltage must be positive")
    if (
        args.passive_cardioid_rg_ohm < 0.0
        or not np.isfinite(args.passive_cardioid_rg_ohm)
    ):
        raise SystemExit("--passive-cardioid-rg-ohm must be non-negative")


def _validate_passive_cardioid_args(args: argparse.Namespace) -> None:
    if args.passive_cardioid_coupled and not args.passive_cardioid_mf:
        raise SystemExit("--passive-cardioid-coupled requires --passive-cardioid-mf")
    if not args.passive_cardioid_mf:
        return
    if args.passive_cardioid_rear_volume_l is None:
        raise SystemExit(
            "--passive-cardioid-rear-volume-l is required with --passive-cardioid-mf"
        )
    if args.passive_cardioid_rear_volume_l <= 0.0:
        raise SystemExit("--passive-cardioid-rear-volume-l must be positive")
    if args.passive_cardioid_port_length_mm is None:
        raise SystemExit(
            "--passive-cardioid-port-length-mm is required with --passive-cardioid-mf"
        )
    if args.passive_cardioid_port_length_mm < 0.0:
        raise SystemExit("--passive-cardioid-port-length-mm must be non-negative")
    if args.passive_cardioid_foam_resistance_pa_s_m3 < 0.0:
        raise SystemExit(
            "--passive-cardioid-foam-resistance-pa-s-m3 must be non-negative"
        )
    _parse_optional_positive_m2_from_cm2(args.passive_cardioid_port_area_cm2)
    if args.passive_cardioid_coupled:
        _validate_passive_cardioid_coupled_args(args)


def _passive_cardioid_driver_from_args(args: argparse.Namespace) -> bandpass.Driver:
    return bandpass.Driver(
        Sd=float(args.passive_cardioid_driver_sd_cm2) * 1.0e-4,
        Bl=float(args.passive_cardioid_driver_bl_tm),
        Re=float(args.passive_cardioid_driver_re_ohm),
        Le=float(args.passive_cardioid_driver_le_mh) * 1.0e-3,
        le2_h=(
            None
            if args.passive_cardioid_driver_le2_mh is None
            else float(args.passive_cardioid_driver_le2_mh) * 1.0e-3
        ),
        re2_ohm=(
            None
            if args.passive_cardioid_driver_re2_ohm is None
            else float(args.passive_cardioid_driver_re2_ohm)
        ),
        Mmd=(
            None
            if args.passive_cardioid_driver_mmd_g is None
            else float(args.passive_cardioid_driver_mmd_g) * 1.0e-3
        ),
        Mms=(
            None
            if args.passive_cardioid_driver_mms_g is None
            else float(args.passive_cardioid_driver_mms_g) * 1.0e-3
        ),
        Cms=(
            None
            if args.passive_cardioid_driver_cms_mm_per_n is None
            else float(args.passive_cardioid_driver_cms_mm_per_n) * 1.0e-3
        ),
        Vas=(
            None
            if args.passive_cardioid_driver_vas_l is None
            else float(args.passive_cardioid_driver_vas_l) * 1.0e-3
        ),
        Fs=(
            None
            if args.passive_cardioid_driver_fs_hz is None
            else float(args.passive_cardioid_driver_fs_hz)
        ),
        Rms=(
            None
            if args.passive_cardioid_driver_rms_kg_per_s is None
            else float(args.passive_cardioid_driver_rms_kg_per_s)
        ),
        Qms=(
            None
            if args.passive_cardioid_driver_qms is None
            else float(args.passive_cardioid_driver_qms)
        ),
        n_drivers=int(args.passive_cardioid_driver_count),
    )


def _write_zma(path: Path, freqs: np.ndarray, impedance: np.ndarray, *, comment: str) -> None:
    lines = [
        f"* {comment}",
        "* Columns: freq_hz |Z|_ohm phase_deg",
    ]
    for freq, z_value in zip(freqs, impedance, strict=True):
        lines.append(
            f"{freq:.6f}\t{abs(z_value):.6f}\t{np.degrees(np.angle(z_value)):.6f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _preferred_passive_cardioid_results(
    passive_payload: dict[str, Any] | None,
) -> tuple[Path, str] | None:
    if passive_payload is None or passive_payload.get("status") != "complete":
        return None
    coupled = passive_payload.get("coupled")
    if isinstance(coupled, dict) and coupled.get("status") == "complete":
        outputs = coupled.get("outputs", {})
        if isinstance(outputs, dict) and outputs.get("results_npz"):
            return Path(outputs["results_npz"]), "passive_cardioid_coupled"
    outputs = passive_payload.get("outputs", {})
    if isinstance(outputs, dict) and outputs.get("results_npz"):
        return Path(outputs["results_npz"]), "passive_cardioid_combined"
    return None


def _solve_passive_cardioid_mf(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_results: list[dict[str, Any]],
    radiation_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not args.passive_cardioid_mf:
        return None

    by_name = _source_result_by_name(source_results)
    mf_name = str(args.passive_cardioid_mf_source)
    requested_port = str(args.passive_cardioid_port_source or "").strip()
    port_name = requested_port or _auto_port_source_name(source_results)
    if mf_name not in by_name:
        return {
            "status": "skipped",
            "reason": (
                "passive-cardioid MF combine requires solved source "
                f"{mf_name!r}"
            ),
            "available_sources": [str(result["name"]) for result in source_results],
        }
    if not port_name or port_name not in by_name:
        return {
            "status": "skipped",
            "reason": (
                "passive-cardioid MF combine requires a solved PORT_EXIT source"
            ),
            "available_sources": [str(result["name"]) for result in source_results],
        }
    if radiation_payload is None:
        return {
            "status": "skipped",
            "reason": (
                "passive-cardioid MF combine requires port-exit radiation "
                "impedance output"
            ),
            "available_sources": [str(result["name"]) for result in source_results],
        }

    mf_basis = _load_pressure_basis(Path(by_name[mf_name]["pressure_basis_npz"]))
    port_basis = _load_pressure_basis(Path(by_name[port_name]["pressure_basis_npz"]))
    try:
        _assert_matching_basis_grid(mf_basis, port_basis)
    except ValueError as exc:
        # Producible configuration outcome (e.g. clamp-per-source trimmed MF
        # but not PORT_EXIT), not a bug: skip with the reason instead of
        # failing the whole run.
        return {
            "status": "skipped",
            "reason": f"MF/PORT_EXIT pressure grids are not combinable: {exc}",
        }

    matrix_path = Path(radiation_payload["outputs"]["npz"])
    load_freqs, termination_load, bem_port_area_m2 = _load_port_exit_termination(
        matrix_path,
        port_source_name=port_name,
    )
    if (
        load_freqs.shape != mf_basis.frequencies_hz.shape
        or not np.allclose(load_freqs, mf_basis.frequencies_hz, rtol=1.0e-8, atol=1.0e-10)
    ):
        return {
            "status": "skipped",
            "reason": (
                "passive-cardioid radiation-impedance frequencies do not "
                "match the MF/PORT_EXIT pressure bases"
            ),
        }

    mf_area_m2 = _mesh_tag_area_m2(
        mesh_path,
        mf_basis.source_tag,
        mesh_scale=float(args.mesh_scale),
    )
    model_port_area_m2 = (
        _parse_optional_positive_m2_from_cm2(args.passive_cardioid_port_area_cm2)
        or bem_port_area_m2
    )
    rear_volume_m3 = float(args.passive_cardioid_rear_volume_l) * 1.0e-3
    port_length_m = float(args.passive_cardioid_port_length_mm) * 1.0e-3
    series_resistance = float(args.passive_cardioid_foam_resistance_pa_s_m3)
    branch = radiation_impedance.terminated_chamber_port_branch(
        mf_basis.frequencies_hz,
        termination_load,
        chamber_volume_m3=rear_volume_m3,
        port_area_m2=model_port_area_m2,
        port_length_m=port_length_m,
        series_resistance_pa_s_m3=series_resistance,
    )

    rear_sign = -1.0 if args.passive_cardioid_invert_port else 1.0
    # Exterior drive: the MF front wave pressurizes the port exit from
    # outside, so the branch equation gains a -j*omega*C*Z(port<-MF)*Q_mf
    # term next to the interior rear drive — in the cardioid band this is
    # the same order as the interior path. Z(port<-MF) exists when the
    # aperture matrix was solved with the MF diaphragm included; older
    # matrices fall back to interior-only drive.
    z_port_mf = _load_port_mf_mutual_impedance(
        matrix_path,
        port_source_name=port_name,
        mf_source_name=mf_name,
    )
    omega = 2.0 * np.pi * np.asarray(mf_basis.frequencies_hz, dtype=np.float64)
    compliance_m3_per_pa = rear_volume_m3 / (
        radiation_impedance.RHO_AIR * radiation_impedance.C_AIR**2
    )
    if z_port_mf is not None:
        exterior_drive = -1j * omega * compliance_m3_per_pa * z_port_mf
    else:
        exterior_drive = np.zeros(omega.shape, dtype=np.complex128)
    port_velocity_weight = (
        (mf_area_m2 / bem_port_area_m2)
        * branch.exit_to_input_volume_velocity_ratio
        * (rear_sign + exterior_drive)
    )
    port_pressure = port_velocity_weight[:, None, None] * port_basis.pressure_complex
    total_pressure = mf_basis.pressure_complex + port_pressure

    # Design diagnostics: the chamber compliance and series resistance form a
    # low-pass on port flow; above f_RC the rear wave compresses the chamber
    # instead of flowing through the foam and the cardioid collapses.
    rc_corner_hz = (
        1.0 / (2.0 * np.pi * series_resistance * compliance_m3_per_pa)
        if series_resistance > 0.0
        else None
    )
    band_lo_hz = float(args.crossover_lf_mf_hz or 200.0)
    band_hi_hz = float(args.crossover_mf_hf_hz or 1000.0)
    q_port_over_q_mf = np.abs(
        branch.exit_to_input_volume_velocity_ratio * (rear_sign + exterior_drive)
    )
    band = (mf_basis.frequencies_hz >= band_lo_hz) & (
        mf_basis.frequencies_hz <= band_hi_hz
    )
    band_min = float(np.min(q_port_over_q_mf[band])) if band.any() else None
    band_max = float(np.max(q_port_over_q_mf[band])) if band.any() else None
    cardioid_diagnostics = {
        "chamber_compliance_m3_per_pa": float(compliance_m3_per_pa),
        "rc_corner_hz": None if rc_corner_hz is None else float(rc_corner_hz),
        "cardioid_band_hz": [band_lo_hz, band_hi_hz],
        "q_port_over_q_mf_band_min": band_min,
        "q_port_over_q_mf_band_max": band_max,
        "exterior_drive_included": z_port_mf is not None,
        "exterior_drive_abs_band_max": (
            float(np.max(np.abs(exterior_drive[band])))
            if (band.any() and z_port_mf is not None)
            else None
        ),
    }
    if band_min is not None and band_min < 0.3:
        corner_text = (
            f"; the chamber/foam corner sits at ~{rc_corner_hz:.0f} Hz"
            if rc_corner_hz is not None
            else ""
        )
        print(
            "PASSIVE CARDIOID NOTE: |Q_port/Q_mf| falls to "
            f"{band_min:.2f} within {band_lo_hz:.0f}-{band_hi_hz:.0f} Hz"
            f"{corner_text}. Port output that weak cannot form a cardioid "
            "null; reduce the rear chamber volume and/or the foam "
            "resistance to push the corner above the band.",
            flush=True,
        )
    directivity_db = _directivity_from_pressure_array(
        total_pressure,
        mf_basis.observation_angles_deg,
    )
    on_axis_idx = int(np.argmin(np.abs(mf_basis.observation_angles_deg)))
    on_axis_spl_db = 20.0 * np.log10(
        np.maximum(np.abs(total_pressure[:, 0, on_axis_idx]), 1.0e-30) / P_REF
    )
    port_on_axis_spl_db = 20.0 * np.log10(
        np.maximum(np.abs(port_pressure[:, 0, on_axis_idx]), 1.0e-30) / P_REF
    )
    mf_on_axis_spl_db = 20.0 * np.log10(
        np.maximum(np.abs(mf_basis.pressure_complex[:, 0, on_axis_idx]), 1.0e-30) / P_REF
    )

    result_npz = out_dir / "MF_passive_cardioid_results.npz"
    heatmap_png = out_dir / "MF_passive_cardioid_directivity_heatmap.png"
    response_png = out_dir / "MF_passive_cardioid_frequency_response.png"
    summary_json = out_dir / "MF_passive_cardioid_summary.json"

    np.savez_compressed(
        result_npz,
        frequencies_hz=mf_basis.frequencies_hz,
        observation_angles_deg=mf_basis.observation_angles_deg,
        observation_planes=mf_basis.observation_planes,
        # total_pressure inherits the loaded bases' engineering convention.
        pressure_complex=total_pressure,
        phase_convention=np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
        directivity_db=directivity_db,
        on_axis_spl_db=on_axis_spl_db,
        mf_on_axis_spl_db=mf_on_axis_spl_db,
        weighted_port_on_axis_spl_db=port_on_axis_spl_db,
        port_velocity_weight=port_velocity_weight,
        exit_to_input_volume_velocity_ratio=branch.exit_to_input_volume_velocity_ratio,
        termination_load=branch.termination_load,
        input_impedance=branch.input_impedance,
        exterior_drive=exterior_drive,
    )
    save_directivity_plot(
        heatmap_png,
        mf_basis.frequencies_hz,
        _directivity_payload_from_arrays(
            mf_basis.observation_angles_deg,
            mf_basis.observation_planes,
            directivity_db,
        ),
        mesh_valid_hz=min(
            value
            for value in (
                by_name[mf_name].get("mesh_valid_freq_max_hz"),
                by_name[port_name].get("mesh_valid_freq_max_hz"),
            )
            if value is not None
        )
        if any(
            value is not None
            for value in (
                by_name[mf_name].get("mesh_valid_freq_max_hz"),
                by_name[port_name].get("mesh_valid_freq_max_hz"),
            )
        )
        else None,
        mesh_valid_radiating_hz=min(
            value
            for value in (
                by_name[mf_name].get("aperture_valid_freq_max_hz"),
                by_name[port_name].get("aperture_valid_freq_max_hz"),
            )
            if value is not None
        )
        if any(
            value is not None
            for value in (
                by_name[mf_name].get("aperture_valid_freq_max_hz"),
                by_name[port_name].get("aperture_valid_freq_max_hz"),
            )
        )
        else None,
    )
    save_frequency_response_plot(
        response_png,
        [
            FrequencyResponseCurve(
                frequencies=mf_basis.frequencies_hz,
                spl_db=mf_on_axis_spl_db,
                label=mf_name,
                role="mf",
            ),
            FrequencyResponseCurve(
                frequencies=mf_basis.frequencies_hz,
                spl_db=port_on_axis_spl_db,
                label=f"{port_name} weighted",
                role="other",
            ),
            FrequencyResponseCurve(
                frequencies=mf_basis.frequencies_hz,
                spl_db=on_axis_spl_db,
                label="MF passive cardioid",
                role="combined",
            ),
        ],
        title="MF Passive Cardioid Combine",
        ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
    )

    payload = {
        "status": "complete",
        "type": "passive_cardioid_mf_combine",
        "sources": {
            "mf": {"name": mf_name, "tag": mf_basis.source_tag},
            "port_exit": {"name": port_name, "tag": port_basis.source_tag},
        },
        "model": {
            "rear_volume_l": float(args.passive_cardioid_rear_volume_l),
            "rear_volume_m3": rear_volume_m3,
            "port_length_mm": float(args.passive_cardioid_port_length_mm),
            "port_length_m": port_length_m,
            "foam_resistance_pa_s_m3": series_resistance,
            "invert_port": bool(args.passive_cardioid_invert_port),
            "rear_drive_sign": rear_sign,
            "port_area_source": (
                "user"
                if args.passive_cardioid_port_area_cm2 is not None
                else "bem_aperture"
            ),
            "model_port_area_m2": model_port_area_m2,
            "bem_port_area_m2": bem_port_area_m2,
            "mf_area_m2": mf_area_m2,
        },
        "diagnostics": cardioid_diagnostics,
        "outputs": {
            "results_npz": str(result_npz),
            "summary_json": str(summary_json),
            "directivity_heatmap_png": str(heatmap_png),
            "frequency_response_png": str(response_png),
        },
        "transfer": {
            "frequencies_hz": mf_basis.frequencies_hz,
            "port_velocity_weight": port_velocity_weight,
            "exit_to_input_volume_velocity_ratio": (
                branch.exit_to_input_volume_velocity_ratio
            ),
            "termination_load": branch.termination_load,
            "input_impedance": branch.input_impedance,
            "exterior_drive": exterior_drive,
        },
    }
    if args.passive_cardioid_coupled:
        mf_impedances = _load_mf_self_and_port_mutual(
            matrix_path,
            mf_source_name=mf_name,
            port_source_name=port_name,
        )
        if mf_impedances is None:
            payload["coupled"] = {
                "status": "skipped",
                "reason": (
                    "passive-cardioid coupled solve requires MF aperture "
                    f"{mf_name!r} in {matrix_path}; this radiation-impedance "
                    "matrix predates the MF-aperture extension"
                ),
            }
        else:
            z_mm, z_mf_from_port = mf_impedances
            driver = _passive_cardioid_driver_from_args(args)
            coupled = driver_coupling.coupled_cardioid_response(
                mf_basis.frequencies_hz,
                driver=driver,
                z_mm=z_mm,
                z_mf_from_port=z_mf_from_port,
                z_port_from_mf=z_port_mf,
                termination_load=termination_load,
                chamber_volume_m3=rear_volume_m3,
                port_area_m2=model_port_area_m2,
                port_length_m=port_length_m,
                series_resistance_pa_s_m3=series_resistance,
                rear_sign=rear_sign,
                drive_voltage_v=float(args.passive_cardioid_drive_voltage),
                rg_ohm=float(args.passive_cardioid_rg_ohm),
            )
            legacy_port_to_cone = (
                branch.exit_to_input_volume_velocity_ratio
                * (rear_sign + exterior_drive)
            )
            ratio_check_passed = bool(
                np.allclose(
                    coupled.port_to_cone_ratio,
                    legacy_port_to_cone,
                    rtol=1.0e-9,
                    atol=1.0e-12,
                )
            )
            ratio_delta = coupled.port_to_cone_ratio - legacy_port_to_cone
            ratio_delta_max = float(np.max(np.abs(ratio_delta)))
            if not ratio_check_passed:
                print(
                    "PASSIVE CARDIOID WARNING: coupled port_to_cone_ratio "
                    "differs from the fixed-velocity branch ratio "
                    f"(max |delta| {ratio_delta_max:.3e}); keeping coupled "
                    "outputs and recording the failed check.",
                    flush=True,
                )

            coupled_pressure = (
                coupled.cone_volume_velocity / mf_area_m2
            )[:, None, None] * total_pressure
            coupled_directivity_db = _directivity_from_pressure_array(
                coupled_pressure,
                mf_basis.observation_angles_deg,
            )
            coupled_on_axis_spl_db = 20.0 * np.log10(
                np.maximum(
                    np.abs(coupled_pressure[:, 0, on_axis_idx]),
                    1.0e-30,
                )
                / P_REF
            )
            coupled_npz = out_dir / "MF_passive_cardioid_coupled_results.npz"
            coupled_response_png = (
                out_dir / "MF_passive_cardioid_coupled_frequency_response.png"
            )
            impedance_zma = out_dir / "MF_passive_cardioid_impedance.zma"
            np.savez_compressed(
                coupled_npz,
                frequencies_hz=mf_basis.frequencies_hz,
                observation_angles_deg=mf_basis.observation_angles_deg,
                observation_planes=mf_basis.observation_planes,
                pressure_complex=coupled_pressure,
                phase_convention=np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
                directivity_db=coupled_directivity_db,
                on_axis_spl_db=coupled_on_axis_spl_db,
                cone_volume_velocity=coupled.cone_volume_velocity,
                port_volume_velocity=coupled.port_volume_velocity,
                port_to_cone_ratio=coupled.port_to_cone_ratio,
                acoustic_load=coupled.acoustic_load,
                electrical_input_impedance=coupled.electrical_input_impedance,
                cone_excursion_m=coupled.cone_excursion_m,
                drive_voltage_v=np.asarray(
                    float(args.passive_cardioid_drive_voltage),
                    dtype=np.float64,
                ),
                rg_ohm=np.asarray(float(args.passive_cardioid_rg_ohm), dtype=np.float64),
                mmd_correction_kg=np.asarray(
                    coupled.mmd_correction_kg,
                    dtype=np.float64,
                ),
            )
            save_frequency_response_plot(
                coupled_response_png,
                [
                    FrequencyResponseCurve(
                        frequencies=mf_basis.frequencies_hz,
                        spl_db=coupled_on_axis_spl_db,
                        label=(
                            f"MF passive cardioid coupled "
                            f"{args.passive_cardioid_drive_voltage:g} V"
                        ),
                        role="combined",
                    )
                ],
                title="MF Passive Cardioid Coupled Response",
                ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
            )
            _write_zma(
                impedance_zma,
                mf_basis.frequencies_hz,
                coupled.electrical_input_impedance,
                comment=(
                    "MF passive-cardioid coupled electrical input impedance "
                    f"at {args.passive_cardioid_drive_voltage:g} V RMS"
                ),
            )

            sd_eff_m2 = float(coupled.diagnostics["sd_eff_m2"])
            sd_vs_mesh_area_pct = 100.0 * (sd_eff_m2 - mf_area_m2) / mf_area_m2
            sd_warning = abs(sd_vs_mesh_area_pct) > 20.0
            if sd_warning:
                print(
                    "PASSIVE CARDIOID WARNING: driver Sd_eff differs from "
                    f"the MF mesh source area by {sd_vs_mesh_area_pct:.1f}%; "
                    "using Sd for electromechanics and mesh area for BEM "
                    "field scaling.",
                    flush=True,
                )
            excursion_band = coupled.cone_excursion_m[band] if band.any() else (
                coupled.cone_excursion_m
            )
            z_abs = np.abs(coupled.electrical_input_impedance)
            coupled_outputs = {
                "results_npz": str(coupled_npz),
                "frequency_response_png": str(coupled_response_png),
                "impedance_zma": str(impedance_zma),
            }
            payload["outputs"]["coupled_results_npz"] = str(coupled_npz)
            payload["outputs"]["coupled_frequency_response_png"] = (
                str(coupled_response_png)
            )
            payload["outputs"]["impedance_zma"] = str(impedance_zma)
            payload["coupled"] = {
                "status": "complete",
                "driver": {
                    "sd_cm2": float(args.passive_cardioid_driver_sd_cm2),
                    "sd_eff_cm2": sd_eff_m2 * 1.0e4,
                    "bl_tm": float(args.passive_cardioid_driver_bl_tm),
                    "re_ohm": float(args.passive_cardioid_driver_re_ohm),
                    "le_mh": float(args.passive_cardioid_driver_le_mh),
                    "le2_mh": args.passive_cardioid_driver_le2_mh,
                    "re2_ohm": args.passive_cardioid_driver_re2_ohm,
                    "mmd_g": args.passive_cardioid_driver_mmd_g,
                    "mms_g": args.passive_cardioid_driver_mms_g,
                    "mmd_eff_g": float(coupled.diagnostics["mmd_eff_kg"]) * 1000.0,
                    "mmd_correction_g": coupled.mmd_correction_kg * 1000.0,
                    "mmd_source": coupled.diagnostics["mmd_source"],
                    "cms_mm_per_n": args.passive_cardioid_driver_cms_mm_per_n,
                    "vas_l": args.passive_cardioid_driver_vas_l,
                    "fs_hz": args.passive_cardioid_driver_fs_hz,
                    "rms_kg_per_s": args.passive_cardioid_driver_rms_kg_per_s,
                    "qms": args.passive_cardioid_driver_qms,
                    "count": int(args.passive_cardioid_driver_count),
                },
                "drive_voltage_v": float(args.passive_cardioid_drive_voltage),
                "rg_ohm": float(args.passive_cardioid_rg_ohm),
                "excursion_band_max_mm": float(np.max(excursion_band) * 1000.0),
                "z_in_elec_min_ohm": float(np.min(z_abs)),
                "z_in_elec_max_ohm": float(np.max(z_abs)),
                "coupled_ratio_check": (
                    "passed" if ratio_check_passed else "failed"
                ),
                "coupled_ratio_check_max_abs_delta": ratio_delta_max,
                "sd_vs_mesh_area_pct": sd_vs_mesh_area_pct,
                "sd_vs_mesh_area_warning": sd_warning,
                "outputs": coupled_outputs,
            }
    _write_json(summary_json, payload)
    return payload


def _build_frame(args: argparse.Namespace) -> ObservationFrame:
    axis = _unit(_parse_vec3(args.frame_axis, name="--frame-axis"), name="--frame-axis")
    u_seed = _parse_vec3(args.frame_u, name="--frame-u")
    u = u_seed - float(np.dot(u_seed, axis)) * axis
    u = _unit(u, name="--frame-u")
    v_arg = args.frame_v.strip()
    if v_arg:
        v_seed = _parse_vec3(v_arg, name="--frame-v")
        v = v_seed - float(np.dot(v_seed, axis)) * axis - float(np.dot(v_seed, u)) * u
        v = _unit(v, name="--frame-v")
    else:
        v = _unit(np.cross(axis, u), name="derived frame v")
    origin = _parse_vec3(args.frame_origin, name="--frame-origin")
    return ObservationFrame(
        axis=axis,
        origin=origin,
        u=u,
        v=v,
        mouth_center=origin.copy(),
        source_center=origin.copy(),
    )


def _build_config(
    args: argparse.Namespace,
    *,
    source_tag: int,
    frame: ObservationFrame,
    freq_max_hz: float | None = None,
) -> SolveConfig:
    native_symmetry_plane = None if args.native_symmetry_plane == "none" else args.native_symmetry_plane
    return SolveConfig(
        freq_min_hz=args.freq_min_hz,
        freq_max_hz=args.freq_max_hz if freq_max_hz is None else freq_max_hz,
        freq_count=args.freq_count,
        freq_spacing=args.freq_spacing,
        velocity_sources={source_tag: 1.0},
        observation=ObservationConfig(
            planes=["horizontal", "vertical"],
            distance_m=args.polar_distance_m,
            angle_min_deg=args.polar_angle_min_deg,
            angle_max_deg=args.polar_angle_max_deg,
            angle_count=args.polar_angle_count,
            origin="mouth",
        ),
        frame_override=frame,
        native_symmetry_plane=native_symmetry_plane,
        native_check_open_edges=args.native_check_open_edges,
        formulation=args.bem_formulation,
        complex_k_shift=args.complex_k_shift,
        metal_native_assembly_mode=args.metal_native_assembly_mode,
        mesh_scale=args.mesh_scale,
    )


def _solve_port_exit_radiation_impedance_matrix(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    apertures: list[tuple[str, int]],
    frame: ObservationFrame,
    source_freq_max: dict[str, float],
    extra_apertures: list[tuple[str, int]] | None = None,
) -> dict[str, Any] | None:
    """Solve the aperture radiation matrix over the PORT_EXIT* apertures.

    ``extra_apertures`` adds non-port apertures (the passive-cardioid MF
    diaphragm) so mutual terms like Z(port <- MF) exist in the matrix; the
    in-phase termination loads keep their port-only meaning regardless.
    """
    if not apertures:
        return None
    extra_apertures = [
        (name, tag)
        for name, tag in (extra_apertures or [])
        if name not in {port_name for port_name, _ in apertures}
    ]
    all_apertures = [*apertures, *extra_apertures]
    freq_max_hz = _radiation_matrix_freq_max_hz(
        args=args,
        apertures=all_apertures,
        source_freq_max=source_freq_max,
    )
    if freq_max_hz < args.freq_min_hz:
        raise ValueError(
            "port-exit radiation impedance frequency limit is below "
            f"--freq-min-hz ({freq_max_hz:.6g} Hz < {args.freq_min_hz:.6g} Hz)"
        )

    freqs = _frequency_grid(
        freq_min_hz=float(args.freq_min_hz),
        freq_max_hz=float(freq_max_hz),
        freq_count=int(args.freq_count),
        freq_spacing=str(args.freq_spacing),
    )
    aperture_tags = {name: [tag] for name, tag in all_apertures}
    config = _build_config(
        args,
        source_tag=all_apertures[0][1],
        frame=frame,
        freq_max_hz=freq_max_hz,
    )
    result = radiation_impedance.solve_aperture_matrix(
        mesh_path,
        aperture_tags,
        freqs,
        config,
    )
    diagnostics = radiation_impedance.matrix_diagnostics(result)
    # In-phase termination loads keep their historical port-only meaning:
    # extra (MF) apertures contribute mutual matrix columns but are excluded
    # from the in-phase reduction consumed by LEM/TMM termination studies.
    port_names = {name for name, _ in apertures}
    port_indices = [
        index
        for index, name in enumerate(result.aperture_names)
        if name in port_names
    ]
    in_phase_loads = np.stack(
        [
            radiation_impedance.termination_load_from_solver_matrix(
                result.impedance_matrix,
                receiver_index=receiver_index,
                source_indices=port_indices,
            )
            for receiver_index in port_indices
        ],
        axis=1,
    )

    npz_path = out_dir / "port_exit_radiation_impedance_matrix.npz"
    summary_path = out_dir / "port_exit_radiation_impedance_matrix.summary.json"
    in_phase_names = [result.aperture_names[index] for index in port_indices]
    np.savez_compressed(
        npz_path,
        frequencies_hz=result.frequencies_hz,
        aperture_names=np.asarray(result.aperture_names),
        aperture_area_m2=np.asarray(
            [result.aperture_area_m2[name] for name in result.aperture_names],
            dtype=np.float64,
        ),
        solver_impedance_matrix=result.impedance_matrix,
        engineering_impedance_matrix=np.conjugate(result.impedance_matrix),
        in_phase_termination_load=in_phase_loads,
        in_phase_aperture_names=np.asarray(in_phase_names),
        reciprocity_max_rel=diagnostics.reciprocity_max_rel,
        passivity_min_eig=diagnostics.passivity_min_eig,
        passivity_ok=diagnostics.passivity_ok,
    )
    payload = _radiation_impedance_payload(
        result=result,
        diagnostics=diagnostics,
        npz_path=npz_path,
        summary_path=summary_path,
        freq_max_hz=freq_max_hz,
        in_phase_loads=in_phase_loads,
        in_phase_names=in_phase_names,
    )
    _write_json(summary_path, payload)
    return payload


def _write_one_source_outputs(
    result,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_name: str,
    source_tag: int,
    freq_max_hz: float | None = None,
    mesh_valid_hz: float | None = None,
    mesh_valid_radiating_hz: float | None = None,
) -> dict[str, Any]:
    safe_name = _safe_stem(source_name)
    result_json = out_dir / f"{safe_name}_results.json"
    basis_npz = out_dir / f"{safe_name}_pressure_basis.npz"
    heatmap_png = out_dir / f"{safe_name}_directivity_heatmap.png"
    response_png = out_dir / f"{safe_name}_frequency_response.png"
    on_axis_spl_db = _on_axis_spl_db(result)
    payload = _result_payload(result, source_name=source_name, source_tag=source_tag)
    _write_json(result_json, payload)
    _write_pressure_basis_npz(
        basis_npz,
        result,
        source_name=source_name,
        source_tag=source_tag,
    )
    save_directivity_plot(
        heatmap_png,
        result.frequencies_hz,
        _result_directivity_payload(result),
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    save_frequency_response_plot(
        response_png,
        [
            FrequencyResponseCurve(
                frequencies=result.frequencies_hz,
                spl_db=on_axis_spl_db,
                label=source_name,
                role=_source_role(source_name),
            )
        ],
        title=f"{source_name} Frequency Response",
        ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    return {
        "name": source_name,
        "tag": source_tag,
        "results_json": str(result_json),
        "pressure_basis_npz": str(basis_npz),
        "directivity_heatmap_png": str(heatmap_png),
        "frequency_response_png": str(response_png),
        "freq_max_hz": float(args.freq_max_hz if freq_max_hz is None else freq_max_hz),
        "mesh_valid_freq_max_hz": None if mesh_valid_hz is None else float(mesh_valid_hz),
        "aperture_valid_freq_max_hz": (
            None if mesh_valid_radiating_hz is None else float(mesh_valid_radiating_hz)
        ),
        "frequencies_hz": result.frequencies_hz,
        "on_axis_spl_db": on_axis_spl_db,
        "timings": result.timings,
    }


def _write_one_source_derived_outputs_from_basis(
    basis_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_name: str,
    source_tag: int,
    mesh_valid_hz: float | None = None,
    mesh_valid_radiating_hz: float | None = None,
    previous_result_json: Path | None = None,
) -> dict[str, Any]:
    if not basis_path.exists():
        raise RuntimeError(
            "postprocess-only requires existing pressure basis for "
            f"{source_name}: {basis_path}"
        )
    basis = _load_pressure_basis(basis_path)
    if int(basis.source_tag) != int(source_tag):
        raise RuntimeError(
            f"postprocess-only source tag mismatch for {source_name}: "
            f"manifest/argv tag {source_tag}, basis tag {basis.source_tag}"
        )
    safe_name = _safe_stem(source_name)
    result_json = out_dir / f"{safe_name}_results.json"
    heatmap_png = out_dir / f"{safe_name}_directivity_heatmap.png"
    response_png = out_dir / f"{safe_name}_frequency_response.png"
    directivity_db = _directivity_from_pressure_array(
        basis.pressure_complex,
        basis.observation_angles_deg,
    )
    on_axis_idx = int(np.argmin(np.abs(basis.observation_angles_deg)))
    on_axis_spl_db = _spl_db_from_pressure(
        basis.pressure_complex[:, 0, on_axis_idx]
    )
    previous_payload: dict[str, Any] = {}
    if previous_result_json is not None and previous_result_json.exists():
        try:
            previous_payload = json.loads(
                previous_result_json.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            previous_payload = {}
    _write_json(
        result_json,
        {
            "source": {"name": source_name, "tag": source_tag},
            "frequencies_hz": basis.frequencies_hz,
            "observation_angles_deg": basis.observation_angles_deg,
            "observation_planes": basis.observation_planes,
            "on_axis_spl_db": on_axis_spl_db,
            "normalized_spl_db": directivity_db,
            "impedance": previous_payload.get("impedance", {}),
            "surface_pressure_avg": previous_payload.get("surface_pressure_avg", {}),
            "timings": previous_payload.get("timings", {}),
            "solver_log": previous_payload.get("solver_log", []),
            "mesh_info": previous_payload.get("mesh_info", {}),
            "postprocess_only_regenerated": True,
        },
    )
    save_directivity_plot(
        heatmap_png,
        basis.frequencies_hz,
        _directivity_payload_from_arrays(
            basis.observation_angles_deg,
            basis.observation_planes,
            directivity_db,
        ),
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    save_frequency_response_plot(
        response_png,
        [
            FrequencyResponseCurve(
                frequencies=basis.frequencies_hz,
                spl_db=on_axis_spl_db,
                label=source_name,
                role=_source_role(source_name),
            )
        ],
        title=f"{source_name} Frequency Response",
        ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    return {
        "name": source_name,
        "tag": source_tag,
        "results_json": str(result_json),
        "pressure_basis_npz": str(basis_path),
        "directivity_heatmap_png": str(heatmap_png),
        "frequency_response_png": str(response_png),
        "freq_max_hz": float(basis.frequencies_hz[-1]),
        "mesh_valid_freq_max_hz": None if mesh_valid_hz is None else float(mesh_valid_hz),
        "aperture_valid_freq_max_hz": (
            None if mesh_valid_radiating_hz is None else float(mesh_valid_radiating_hz)
        ),
        "frequencies_hz": basis.frequencies_hz,
        "on_axis_spl_db": on_axis_spl_db,
        "timings": previous_payload.get("timings", {}),
        "postprocess_only": True,
    }


def _solve_one_source(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_name: str,
    source_tag: int,
    frame: ObservationFrame,
    freq_max_hz: float | None = None,
    mesh_valid_hz: float | None = None,
    mesh_valid_radiating_hz: float | None = None,
) -> dict[str, Any]:
    config = _build_config(args, source_tag=source_tag, frame=frame, freq_max_hz=freq_max_hz)
    result = solve(str(mesh_path), config)
    return _write_one_source_outputs(
        result,
        out_dir,
        args,
        source_name=source_name,
        source_tag=source_tag,
        freq_max_hz=freq_max_hz,
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )


def _solve_source_group(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    group: list[tuple[str, int]],
    frame: ObservationFrame,
    freq_max_hz: float | None = None,
    mesh_valid: dict[str, float] | None = None,
    aperture_valid: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Solve several sources sharing one frequency grid in ONE multi-RHS call.

    The mesh is identical for every source (tagged_sources.msh carries all
    source tags), so the native helper assembles and factors each frequency's
    operator once and back-substitutes one RHS per source instead of running
    one full solve per source. Results match sequential per-source solves to
    float32 tolerance; the shared solve wall time is recorded on every
    source's manifest entry as ``shared_solve_wall_s``.
    """
    mesh_valid = mesh_valid or {}
    aperture_valid = aperture_valid or {}
    config = _build_config(
        args, source_tag=group[0][1], frame=frame, freq_max_hz=freq_max_hz
    )
    group_start = time.time()
    results = solve_multi_source(
        str(mesh_path),
        [{tag: 1.0} for _, tag in group],
        config,
    )
    shared_wall_s = time.time() - group_start
    source_results = []
    for (source_name, source_tag), result in zip(group, results):
        source_result = _write_one_source_outputs(
            result,
            out_dir,
            args,
            source_name=source_name,
            source_tag=source_tag,
            freq_max_hz=freq_max_hz,
            mesh_valid_hz=mesh_valid.get(source_name),
            mesh_valid_radiating_hz=aperture_valid.get(source_name),
        )
        source_result["multi_source_group"] = [name for name, _ in group]
        source_result["shared_solve_wall_s"] = shared_wall_s
        source_results.append(source_result)
    return source_results


_RADIATION_PAYLOAD_FROM_SOLVER = object()


def _apply_post_solve_derived_outputs(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    source_results: list[dict[str, Any]],
    sources: list[tuple[str, int]],
    port_exit_apertures: list[tuple[str, int]],
    source_freq_max: dict[str, float],
    source_mesh_valid: dict[str, float],
    source_aperture_valid: dict[str, float],
    frame: ObservationFrame,
    radiation_payload_override: object = _RADIATION_PAYLOAD_FROM_SOLVER,
    current_phase_prefix: str = "",
) -> None:
    source_index_by_name = {name: idx for idx, (name, _) in enumerate(sources)}
    response_curves = [
        FrequencyResponseCurve(
            frequencies=source_result["frequencies_hz"],
            spl_db=source_result["on_axis_spl_db"],
            label=source_result["name"],
            role=_source_role(source_result["name"]),
        )
        for source_result in source_results
    ]
    source_results.sort(key=lambda entry: source_index_by_name.get(entry["name"], 9999))
    response_curves.sort(key=lambda curve: source_index_by_name.get(curve.label, 9999))

    combined_mesh_valid_hz = min(source_mesh_valid.values()) if source_mesh_valid else None
    combined_aperture_valid_hz = (
        min(source_aperture_valid.values()) if source_aperture_valid else None
    )
    response_png = out_dir / "combined_frequency_response.png"
    save_frequency_response_plot(
        response_png,
        response_curves,
        title="Fusion WG Metal Direct Source Responses",
        ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
        mesh_valid_hz=combined_mesh_valid_hz,
        mesh_valid_radiating_hz=combined_aperture_valid_hz,
    )
    manifest["sources"] = source_results
    manifest["outputs"]["combined_frequency_response_png"] = str(response_png)
    manifest["outputs"]["source_frequency_response_pngs"] = {
        source["name"]: source["frequency_response_png"] for source in source_results
    }
    manifest["outputs"]["source_pressure_basis_npzs"] = {
        source["name"]: source["pressure_basis_npz"] for source in source_results
    }

    def _run_crossover_sum(
        mf_override_npz: Path | None,
        mf_override_kind: str,
    ) -> dict[str, Any] | None:
        crossover_payload = _write_crossover_alignment_outputs(
            out_dir,
            source_results,
            lf_mf_hz=args.crossover_lf_mf_hz,
            mf_hf_hz=args.crossover_mf_hf_hz,
            polar_distance_m=float(args.polar_distance_m),
            mesh_valid_hz=combined_mesh_valid_hz,
            mesh_valid_radiating_hz=combined_aperture_valid_hz,
            mf_override_npz=mf_override_npz,
            mf_override_kind=mf_override_kind,
        )
        if crossover_payload is not None:
            manifest["crossover_alignment"] = crossover_payload
            outputs = crossover_payload.get("outputs", {})
            if crossover_payload.get("status") == "complete" and isinstance(outputs, dict):
                manifest["outputs"].update(outputs)
        return crossover_payload

    active_crossover_payload = _run_crossover_sum(None, "direct")
    _update_manifest(
        manifest_path,
        manifest,
        status="running",
        current_phase=f"{current_phase_prefix}radiation_impedance",
        current_source=None,
    )

    passive_mf_apertures: list[tuple[str, int]] = []
    if args.passive_cardioid_mf:
        wanted_mf = str(args.passive_cardioid_mf_source).strip().upper()
        passive_mf_apertures = [
            (name, tag)
            for name, tag in sources
            if name.strip().upper() == wanted_mf
        ]
    if radiation_payload_override is _RADIATION_PAYLOAD_FROM_SOLVER:
        radiation_payload = _solve_port_exit_radiation_impedance_matrix(
            mesh_path,
            out_dir,
            args,
            apertures=port_exit_apertures,
            frame=frame,
            source_freq_max=source_freq_max,
            extra_apertures=passive_mf_apertures,
        )
    else:
        radiation_payload = radiation_payload_override
    if radiation_payload is not None:
        manifest["radiation_impedance"] = radiation_payload
        outputs = radiation_payload.get("outputs", {})
        if isinstance(outputs, dict) and outputs.get("npz"):
            manifest["outputs"]["port_exit_radiation_impedance_npz"] = outputs["npz"]
        if isinstance(outputs, dict) and outputs.get("summary_json"):
            manifest["outputs"]["port_exit_radiation_impedance_summary_json"] = (
                outputs["summary_json"]
            )
    passive_payload = _solve_passive_cardioid_mf(
        mesh_path,
        out_dir,
        args,
        source_results=source_results,
        radiation_payload=radiation_payload,
    )
    if passive_payload is not None:
        manifest["passive_cardioid"] = passive_payload
        outputs = passive_payload.get("outputs", {})
        if passive_payload.get("status") == "complete" and isinstance(outputs, dict):
            manifest["outputs"]["passive_cardioid_results_npz"] = (
                outputs["results_npz"]
            )
            manifest["outputs"]["passive_cardioid_summary_json"] = (
                outputs["summary_json"]
            )
            manifest["outputs"]["passive_cardioid_directivity_heatmap_png"] = (
                outputs["directivity_heatmap_png"]
            )
            manifest["outputs"]["passive_cardioid_frequency_response_png"] = (
                outputs["frequency_response_png"]
            )
            if outputs.get("coupled_results_npz"):
                manifest["outputs"]["passive_cardioid_coupled_results_npz"] = (
                    outputs["coupled_results_npz"]
                )
            if outputs.get("coupled_frequency_response_png"):
                manifest["outputs"][
                    "passive_cardioid_coupled_frequency_response_png"
                ] = outputs["coupled_frequency_response_png"]
            if outputs.get("impedance_zma"):
                manifest["outputs"]["passive_cardioid_impedance_zma"] = (
                    outputs["impedance_zma"]
                )
    if (
        passive_payload is not None
        and passive_payload.get("status") == "complete"
        and str(args.passive_cardioid_mf_source).strip().upper() == "MF"
    ):
        override = _preferred_passive_cardioid_results(passive_payload)
        if override is not None:
            override_payload = _run_crossover_sum(*override)
            if (
                isinstance(override_payload, dict)
                and override_payload.get("status") == "complete"
            ):
                active_crossover_payload = override_payload
    if args.export_vituixcad:
        vituixcad_payload = _write_vituixcad_export(
            out_dir,
            source_results,
            polar_distance_m=float(args.polar_distance_m),
            passive_payload=passive_payload,
            active_crossover_payload=active_crossover_payload,
        )
        if vituixcad_payload is not None:
            manifest["vituixcad_export"] = vituixcad_payload
            manifest["outputs"].update(vituixcad_payload["outputs"])


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_path_in_run(
    value: Any,
    out_dir: Path,
    *,
    fallback_name: str,
) -> Path:
    candidates: list[Path] = []
    if value:
        path = Path(str(value))
        candidates.append(path)
        if not path.is_absolute():
            candidates.append(out_dir / path)
        candidates.append(out_dir / path.name)
    candidates.append(out_dir / fallback_name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def _source_entries_from_manifests(
    direct_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = [
        direct_manifest.get("sources"),
        final_manifest.get("direct_solve", {}).get("sources"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            entries = [
                item
                for item in candidate
                if isinstance(item, dict)
                and item.get("name") is not None
                and item.get("tag") is not None
            ]
            if entries:
                return entries
    return []


def _sources_from_previous_manifests(
    direct_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
) -> list[tuple[str, int]]:
    sources: list[tuple[str, int]] = []
    for entry in _source_entries_from_manifests(direct_manifest, final_manifest):
        if entry.get("pressure_basis_npz") or entry.get("status") in {None, "complete"}:
            sources.append((str(entry["name"]), int(entry["tag"])))
    return sources


def _load_existing_radiation_payload(
    out_dir: Path,
    *,
    direct_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
    required: bool,
) -> dict[str, Any] | None:
    payloads = [
        direct_manifest.get("radiation_impedance"),
        final_manifest.get("direct_solve", {}).get("radiation_impedance"),
    ]
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        outputs = payload.get("outputs", {})
        if not isinstance(outputs, dict):
            continue
        npz_path = _manifest_path_in_run(
            outputs.get("npz"),
            out_dir,
            fallback_name="port_exit_radiation_impedance_matrix.npz",
        )
        if npz_path.exists():
            rebuilt = dict(payload)
            rebuilt_outputs = dict(outputs)
            rebuilt_outputs["npz"] = str(npz_path)
            if rebuilt_outputs.get("summary_json"):
                rebuilt_outputs["summary_json"] = str(
                    _manifest_path_in_run(
                        rebuilt_outputs["summary_json"],
                        out_dir,
                        fallback_name="port_exit_radiation_impedance_matrix.summary.json",
                    )
                )
            rebuilt["outputs"] = rebuilt_outputs
            return rebuilt
    summary_path = out_dir / "port_exit_radiation_impedance_matrix.summary.json"
    if summary_path.exists():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        outputs = payload.setdefault("outputs", {})
        outputs["npz"] = str(
            _manifest_path_in_run(
                outputs.get("npz"),
                out_dir,
                fallback_name="port_exit_radiation_impedance_matrix.npz",
            )
        )
        outputs["summary_json"] = str(summary_path)
        if Path(outputs["npz"]).exists():
            return payload
    npz_path = out_dir / "port_exit_radiation_impedance_matrix.npz"
    if npz_path.exists():
        return {
            "status": "complete",
            "type": "port_exit_radiation_impedance_matrix",
            "outputs": {
                "npz": str(npz_path),
                "summary_json": str(summary_path),
            },
        }
    if required:
        raise RuntimeError(
            "postprocess-only requires existing "
            f"port_exit_radiation_impedance_matrix.npz in {out_dir}"
        )
    return None


def _update_pipeline_summary_manifests(out_dir: Path, direct_manifest: dict[str, Any]) -> None:
    for path in (
        out_dir / "fusion_wg_pipeline_manifest.json",
        out_dir / "final_summary_manifest.json",
    ):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload["direct_solve"] = direct_manifest
            payload["status"] = "complete"
            payload["finished_at"] = direct_manifest.get(
                "finished_at",
                datetime.now().isoformat(timespec="seconds"),
            )
            _write_json(path, payload)


def _run_postprocess_only(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    sources: list[tuple[str, int]],
    port_exit_apertures: list[tuple[str, int]],
    source_freq_max: dict[str, float],
    source_mesh_valid: dict[str, float],
    source_aperture_valid: dict[str, float],
    frame: ObservationFrame,
    direct_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
) -> None:
    if not direct_manifest and not final_manifest:
        raise RuntimeError(
            "postprocess-only requires direct_solve_manifest.json or "
            "final_summary_manifest.json in the run folder"
        )
    source_entries = {
        str(entry["name"]): entry
        for entry in _source_entries_from_manifests(direct_manifest, final_manifest)
    }
    source_results: list[dict[str, Any]] = []
    for source_name, source_tag in sources:
        entry = source_entries.get(source_name, {})
        basis_path = _manifest_path_in_run(
            entry.get("pressure_basis_npz"),
            out_dir,
            fallback_name=f"{_safe_stem(source_name)}_pressure_basis.npz",
        )
        previous_result_json = _manifest_path_in_run(
            entry.get("results_json"),
            out_dir,
            fallback_name=f"{_safe_stem(source_name)}_results.json",
        )
        source_results.append(
            _write_one_source_derived_outputs_from_basis(
                basis_path,
                out_dir,
                args,
                source_name=source_name,
                source_tag=source_tag,
                mesh_valid_hz=(
                    entry.get("mesh_valid_freq_max_hz")
                    if entry.get("mesh_valid_freq_max_hz") is not None
                    else source_mesh_valid.get(source_name)
                ),
                mesh_valid_radiating_hz=(
                    entry.get("aperture_valid_freq_max_hz")
                    if entry.get("aperture_valid_freq_max_hz") is not None
                    else source_aperture_valid.get(source_name)
                ),
                previous_result_json=previous_result_json,
            )
        )
    # The matrix artifact exists only when the original run solved a
    # PORT_EXIT aperture group; cardioid flags alone never produced one (the
    # combine skipped instead), so postprocess-only must mirror that skip
    # rather than demand a file the original run never wrote.
    radiation_required = bool(port_exit_apertures)
    radiation_payload = _load_existing_radiation_payload(
        out_dir,
        direct_manifest=direct_manifest,
        final_manifest=final_manifest,
        required=radiation_required,
    )
    _apply_post_solve_derived_outputs(
        mesh_path,
        out_dir,
        args,
        manifest=manifest,
        manifest_path=manifest_path,
        source_results=source_results,
        sources=sources,
        port_exit_apertures=port_exit_apertures,
        source_freq_max=source_freq_max,
        source_mesh_valid=source_mesh_valid,
        source_aperture_valid=source_aperture_valid,
        frame=frame,
        radiation_payload_override=radiation_payload,
        current_phase_prefix="postprocess_only_",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Source spec NAME:TAG or NAME:RES_MM:TAG. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--source-freq-max",
        action="append",
        default=[],
        help=(
            "Per-source solve frequency limit NAME:HZ. Overrides --freq-max-hz "
            "for that source only. May be repeated."
        ),
    )
    parser.add_argument(
        "--source-mesh-valid-hz",
        action="append",
        default=[],
        help=(
            "Per-source conservative (fully-resolved) mesh-valid frequency "
            "NAME:HZ, overlaid as a SOLID vertical marker on that source's "
            "plots. Does not change the solved band (overlay only). Repeatable."
        ),
    )
    parser.add_argument(
        "--source-aperture-valid-hz",
        action="append",
        default=[],
        help=(
            "Per-source radiating-aperture mesh-valid frequency NAME:HZ, "
            "overlaid as a DASHED vertical marker; the band between it and the "
            "solid line is where only the aperture is resolved. Overlay only. "
            "Repeatable."
        ),
    )
    parser.add_argument("--freq-min-hz", type=float, default=50.0)
    parser.add_argument("--freq-max-hz", type=float, default=20_000.0)
    parser.add_argument("--freq-count", type=int, default=60)
    parser.add_argument("--freq-spacing", choices=("log", "linear"), default="log")
    parser.add_argument(
        "--crossover-lf-mf-hz",
        type=float,
        default=None,
        help=(
            "Optional LF/MF LR4 crossover frequency. With two of LF/MF/HF "
            "solved, one crossover frequency builds a two-way time-aligned "
            "sum; with all three solved, both crossover frequencies build "
            "the three-way sum (plots, interference heatmap, and "
            "driver_time_alignment.txt)."
        ),
    )
    parser.add_argument(
        "--crossover-mf-hf-hz",
        type=float,
        default=None,
        help=(
            "Optional MF/HF LR4 crossover frequency for the time-aligned "
            "crossover sum (see --crossover-lf-mf-hz)."
        ),
    )
    parser.add_argument("--polar-distance-m", type=float, default=2.0)
    parser.add_argument("--polar-angle-min-deg", type=float, default=0.0)
    parser.add_argument("--polar-angle-max-deg", type=float, default=180.0)
    parser.add_argument("--polar-angle-count", type=int, default=37)
    parser.add_argument(
        "--export-vituixcad",
        action="store_true",
        help=(
            "Write per-driver per-angle FRD sets (vituixcad/hor, "
            "vituixcad/ver) with a shared timing reference for VituixCAD "
            "crossover design; also writes HornLab_active_lr4.vxp when the "
            "LR4 crossover alignment completes. Includes the "
            "passive-cardioid combined MF when that combine runs."
        ),
    )
    parser.add_argument("--mesh-scale", type=float, default=0.001)
    parser.add_argument(
        "--native-symmetry-plane",
        choices=("none", "yz", "xz", "xy", "yz+xz"),
        default="yz+xz",
    )
    parser.add_argument(
        "--native-check-open-edges",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enforce that a reduced-domain symmetry mesh has every open boundary "
            "edge on a requested plane. Pass --no-native-check-open-edges for a "
            "bare (open-mouth) horn whose mirror-reduced rim is a real free edge."
        ),
    )
    parser.add_argument("--frame-axis", default="+Z")
    parser.add_argument("--frame-origin", default="0,0,0.31")
    parser.add_argument("--frame-u", default="+X")
    parser.add_argument("--frame-v", default="+Y")
    parser.add_argument(
        "--bem-formulation",
        type=_normalize_bem_formulation,
        default="complex_k",
        metavar="{standard,complex_k,complex-k}",
        help="BEM formulation for direct Metal solves. Defaults to complex_k.",
    )
    parser.add_argument(
        "--complex-k-shift",
        type=float,
        default=0.005,
        help="Imaginary wavenumber shift used when --bem-formulation=complex_k.",
    )
    parser.add_argument(
        "--metal-native-assembly-mode",
        choices=("corrected", "optimized"),
        default="corrected",
    )
    parser.add_argument(
        "--passive-cardioid-mf",
        action="store_true",
        help=(
            "After solving MF and PORT_EXIT bases, combine them with a "
            "lumped rear-chamber/resistive-port transfer."
        ),
    )
    parser.add_argument("--passive-cardioid-mf-source", default="MF")
    parser.add_argument("--passive-cardioid-port-source", default="")
    parser.add_argument("--passive-cardioid-rear-volume-l", type=float, default=None)
    parser.add_argument("--passive-cardioid-port-length-mm", type=float, default=None)
    parser.add_argument("--passive-cardioid-port-area-cm2", type=float, default=None)
    parser.add_argument(
        "--passive-cardioid-foam-resistance-pa-s-m3",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--passive-cardioid-invert-port",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Treat the port as driven by the MF rear wave, opposite the MF "
            "front source polarity. Pass --no-passive-cardioid-invert-port "
            "if the result cancels the wrong side."
        ),
    )
    parser.add_argument("--passive-cardioid-coupled", action="store_true")
    parser.add_argument("--passive-cardioid-driver-sd-cm2", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-bl-tm", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-re-ohm", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-le-mh", type=float, default=0.0)
    parser.add_argument("--passive-cardioid-driver-le2-mh", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-re2-ohm", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-mmd-g", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-mms-g", type=float, default=None)
    parser.add_argument(
        "--passive-cardioid-driver-cms-mm-per-n",
        type=float,
        default=None,
    )
    parser.add_argument("--passive-cardioid-driver-vas-l", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-fs-hz", type=float, default=None)
    parser.add_argument(
        "--passive-cardioid-driver-rms-kg-per-s",
        type=float,
        default=None,
    )
    parser.add_argument("--passive-cardioid-driver-qms", type=float, default=None)
    parser.add_argument("--passive-cardioid-driver-count", type=int, default=1)
    parser.add_argument("--passive-cardioid-drive-voltage", type=float, default=2.83)
    parser.add_argument("--passive-cardioid-rg-ohm", type=float, default=0.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate options and write a manifest without solving",
    )
    parser.add_argument(
        "--postprocess-only",
        action="store_true",
        help=(
            "Regenerate derived artifacts from an existing run folder without "
            "running BEM solves or rewriting pressure-basis/radiation NPZs."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mesh_path = args.mesh.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not mesh_path.exists() and not args.postprocess_only:
        raise SystemExit(f"mesh not found: {mesh_path}")
    previous_direct_manifest = _read_json_if_exists(out_dir / "direct_solve_manifest.json")
    previous_final_manifest = _read_json_if_exists(out_dir / "final_summary_manifest.json")
    sources = _split_sources(args.source)
    if args.postprocess_only and not sources:
        sources = _sources_from_previous_manifests(
            previous_direct_manifest,
            previous_final_manifest,
        )
    if not sources:
        raise SystemExit("at least one --source is required")
    if len({tag for _, tag in sources}) != len(sources):
        raise SystemExit("source tags must be unique")
    sources = _order_sources_for_solves(sources)
    port_exit_apertures = _port_exit_apertures(sources)
    source_freq_max = _parse_source_freq_max(args.source_freq_max)
    source_mesh_valid = _parse_source_freq_max(args.source_mesh_valid_hz)
    source_aperture_valid = _parse_source_freq_max(args.source_aperture_valid_hz)
    source_names = {name for name, _ in sources}
    unknown_limits = sorted(set(source_freq_max) - source_names)
    if unknown_limits:
        raise SystemExit(
            f"--source-freq-max names not in --source list: {', '.join(unknown_limits)}"
        )
    unknown_overlays = sorted(
        (set(source_mesh_valid) | set(source_aperture_valid)) - source_names
    )
    if unknown_overlays:
        raise SystemExit(
            f"mesh-valid overlay names not in --source list: {', '.join(unknown_overlays)}"
        )
    if args.freq_min_hz <= 0.0 or args.freq_max_hz <= 0.0:
        raise SystemExit("frequency bounds must be positive")
    if args.freq_max_hz < args.freq_min_hz:
        raise SystemExit("--freq-max-hz must be >= --freq-min-hz")
    for name, freq_max_hz in source_freq_max.items():
        if freq_max_hz < args.freq_min_hz:
            raise SystemExit(
                f"--source-freq-max for {name} is below --freq-min-hz "
                f"({freq_max_hz:.6g} Hz < {args.freq_min_hz:.6g} Hz)"
            )
    if args.freq_count <= 0:
        raise SystemExit("--freq-count must be positive")
    for flag, value in (
        ("--crossover-lf-mf-hz", args.crossover_lf_mf_hz),
        ("--crossover-mf-hf-hz", args.crossover_mf_hf_hz),
    ):
        if value is None:
            continue
        if value <= 0.0:
            raise SystemExit(f"{flag} must be positive")
        if not (args.freq_min_hz <= value <= args.freq_max_hz):
            raise SystemExit(f"{flag} must be within the solved band")
    if (
        args.crossover_lf_mf_hz is not None
        and args.crossover_mf_hf_hz is not None
        and args.crossover_lf_mf_hz >= args.crossover_mf_hf_hz
    ):
        raise SystemExit("--crossover-lf-mf-hz must be below --crossover-mf-hf-hz")
    if args.polar_distance_m <= 0.0:
        raise SystemExit("--polar-distance-m must be positive")
    if args.polar_angle_count <= 0:
        raise SystemExit("--polar-angle-count must be positive")
    if args.complex_k_shift < 0.0:
        raise SystemExit("--complex-k-shift must be non-negative")
    _validate_passive_cardioid_args(args)

    frame = _build_frame(args)
    started_at = datetime.now().isoformat(timespec="seconds")
    manifest: dict[str, Any] = {
        "pipeline": "solve_fusion_wg_metal",
        "started_at": started_at,
        "mesh": str(mesh_path),
        "output_dir": str(out_dir),
        "dry_run": bool(args.dry_run),
        "postprocess_only": bool(args.postprocess_only),
        "frame": {
            "axis": frame.axis,
            "origin": frame.origin,
            "u": frame.u,
            "v": frame.v,
            "native_symmetry_plane": None if args.native_symmetry_plane == "none" else args.native_symmetry_plane,
            "native_check_open_edges": bool(args.native_check_open_edges),
        },
        "config": {
            "freq_min_hz": args.freq_min_hz,
            "freq_max_hz": args.freq_max_hz,
            "freq_count": args.freq_count,
            "freq_spacing": args.freq_spacing,
            "crossover": {
                "lf_mf_hz": args.crossover_lf_mf_hz,
                "mf_hf_hz": args.crossover_mf_hf_hz,
                "type": "lr4",
            },
            "polar_distance_m": args.polar_distance_m,
            "polar_angle_min_deg": args.polar_angle_min_deg,
            "polar_angle_max_deg": args.polar_angle_max_deg,
            "polar_angle_count": args.polar_angle_count,
            "mesh_scale": args.mesh_scale,
            "bem_formulation": args.bem_formulation,
            "complex_k_shift": args.complex_k_shift,
            "metal_native_assembly_mode": args.metal_native_assembly_mode,
            "solver_package": "hornlab_metal_bem",
            "solver_dir": str(METAL_BEM_DIR),
            "linear_solver": "native-dense-cgesv",
            "source_freq_max_hz": dict(source_freq_max),
            "passive_cardioid": {
                "enabled": bool(args.passive_cardioid_mf),
                "mf_source": args.passive_cardioid_mf_source,
                "port_source": args.passive_cardioid_port_source or "auto",
                "rear_volume_l": args.passive_cardioid_rear_volume_l,
                "port_length_mm": args.passive_cardioid_port_length_mm,
                "port_area_cm2": args.passive_cardioid_port_area_cm2,
                "foam_resistance_pa_s_m3": (
                    args.passive_cardioid_foam_resistance_pa_s_m3
                ),
                "invert_port": bool(args.passive_cardioid_invert_port),
            },
        },
        "sources": [],
        "radiation_impedance": {
            "status": "planned" if port_exit_apertures else "not_requested",
            "trigger": "PORT_EXIT* source tags",
            "apertures": [
                {"name": name, "tag": tag}
                for name, tag in port_exit_apertures
            ],
        },
        "outputs": {},
        "status": "dry_run" if args.dry_run else "running",
    }
    if args.passive_cardioid_coupled:
        manifest["config"]["passive_cardioid"]["coupled"] = True
        manifest["config"]["passive_cardioid"]["drive_voltage_v"] = (
            args.passive_cardioid_drive_voltage
        )
        manifest["config"]["passive_cardioid"]["rg_ohm"] = args.passive_cardioid_rg_ohm
        manifest["config"]["passive_cardioid"]["driver"] = {
            "sd_cm2": args.passive_cardioid_driver_sd_cm2,
            "bl_tm": args.passive_cardioid_driver_bl_tm,
            "re_ohm": args.passive_cardioid_driver_re_ohm,
            "le_mh": args.passive_cardioid_driver_le_mh,
            "le2_mh": args.passive_cardioid_driver_le2_mh,
            "re2_ohm": args.passive_cardioid_driver_re2_ohm,
            "mmd_g": args.passive_cardioid_driver_mmd_g,
            "mms_g": args.passive_cardioid_driver_mms_g,
            "cms_mm_per_n": args.passive_cardioid_driver_cms_mm_per_n,
            "vas_l": args.passive_cardioid_driver_vas_l,
            "fs_hz": args.passive_cardioid_driver_fs_hz,
            "rms_kg_per_s": args.passive_cardioid_driver_rms_kg_per_s,
            "qms": args.passive_cardioid_driver_qms,
            "count": args.passive_cardioid_driver_count,
        }
    manifest_path = out_dir / "direct_solve_manifest.json"

    if args.dry_run:
        manifest["sources"] = [
            {"name": name, "tag": tag}
            for name, tag in sources
        ]
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(manifest_path, manifest)
        print(json.dumps(_jsonable(manifest), indent=2, sort_keys=True))
        return 0

    if args.postprocess_only:
        manifest["sources"] = [
            {"name": name, "tag": tag, "status": "postprocess_pending"}
            for name, tag in sources
        ]
        _update_manifest(
            manifest_path,
            manifest,
            status="running",
            current_phase="postprocess_only_reconstructing_sources",
        )
        try:
            _run_postprocess_only(
                mesh_path,
                out_dir,
                args,
                manifest=manifest,
                manifest_path=manifest_path,
                sources=sources,
                port_exit_apertures=port_exit_apertures,
                source_freq_max=source_freq_max,
                source_mesh_valid=source_mesh_valid,
                source_aperture_valid=source_aperture_valid,
                frame=frame,
                direct_manifest=previous_direct_manifest,
                final_manifest=previous_final_manifest,
            )
            manifest["status"] = "complete"
            manifest["current_phase"] = "complete"
            manifest.pop("current_source", None)
            returncode = 0
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["error"] = f"{type(exc).__name__}: {exc}"
            returncode = 1
        finally:
            manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _write_json(manifest_path, manifest)
            if manifest.get("status") == "complete":
                _update_pipeline_summary_manifests(out_dir, manifest)
        print(json.dumps(_jsonable(manifest), indent=2, sort_keys=True))
        return returncode

    source_progress: list[dict[str, Any]] = [
        {"name": name, "tag": tag, "status": "pending"}
        for name, tag in sources
    ]
    manifest["sources"] = source_progress
    _update_manifest(
        manifest_path,
        manifest,
        current_phase="initializing_direct_solve",
    )

    source_results = []
    lock_file = None
    try:
        lock_file = _acquire_direct_solve_lock(manifest_path, manifest)
        # Sources sharing an effective frequency band share one mesh and one
        # frequency grid, so they can ride one multi-RHS native solve (one
        # assembly+factorization per frequency) instead of N full solves.
        source_index_by_name = {name: idx for idx, (name, _) in enumerate(sources)}
        solve_groups: list[tuple[float, list[tuple[str, int]]]] = []
        for source_name, source_tag in sources:
            effective_fmax = float(source_freq_max.get(source_name, args.freq_max_hz))
            for group_fmax, group in solve_groups:
                if group_fmax == effective_fmax:
                    group.append((source_name, source_tag))
                    break
            else:
                solve_groups.append((effective_fmax, [(source_name, source_tag)]))

        for group_fmax, group in solve_groups:
            for source_name, source_tag in group:
                source_progress[source_index_by_name[source_name]].update(
                    {
                        "status": "running",
                        "started_at": datetime.now().isoformat(timespec="seconds"),
                        "freq_max_hz": group_fmax,
                    }
                )
            _update_manifest(
                manifest_path,
                manifest,
                status="running",
                current_phase="solving_source",
                current_source={
                    "name": group[0][0],
                    "tag": group[0][1],
                    **(
                        {"group": [name for name, _ in group]}
                        if len(group) > 1
                        else {}
                    ),
                },
            )
            if len(group) == 1:
                source_name, source_tag = group[0]
                print(f"Solving source {source_name} (tag {source_tag})...", flush=True)
                group_results = [
                    _solve_one_source(
                        mesh_path,
                        out_dir,
                        args,
                        source_name=source_name,
                        source_tag=source_tag,
                        frame=frame,
                        freq_max_hz=source_freq_max.get(source_name),
                        mesh_valid_hz=source_mesh_valid.get(source_name),
                        mesh_valid_radiating_hz=source_aperture_valid.get(source_name),
                    )
                ]
            else:
                group_label = ", ".join(name for name, _ in group)
                print(
                    f"Solving sources {group_label} in one multi-RHS call...",
                    flush=True,
                )
                group_results = _solve_source_group(
                    mesh_path,
                    out_dir,
                    args,
                    group=group,
                    frame=frame,
                    freq_max_hz=group_fmax,
                    mesh_valid=source_mesh_valid,
                    aperture_valid=source_aperture_valid,
                )
            for source_result in group_results:
                source_name = str(source_result["name"])
                source_tag = int(source_result["tag"])
                source_results.append(source_result)
                source_progress[source_index_by_name[source_name]] = {
                    "name": source_name,
                    "tag": source_tag,
                    "status": "complete",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "results_json": source_result["results_json"],
                    "pressure_basis_npz": source_result["pressure_basis_npz"],
                    "directivity_heatmap_png": source_result["directivity_heatmap_png"],
                    "frequency_response_png": source_result["frequency_response_png"],
                    "freq_max_hz": source_result["freq_max_hz"],
                    "mesh_valid_freq_max_hz": source_result["mesh_valid_freq_max_hz"],
                    "aperture_valid_freq_max_hz": source_result[
                        "aperture_valid_freq_max_hz"
                    ],
                }
                _update_manifest(
                    manifest_path,
                    manifest,
                    status="running",
                    current_phase="source_complete",
                    current_source={"name": source_name, "tag": source_tag},
                )
                print(f"Completed source {source_name} (tag {source_tag}).", flush=True)
        _apply_post_solve_derived_outputs(
            mesh_path,
            out_dir,
            args,
            manifest=manifest,
            manifest_path=manifest_path,
            source_results=source_results,
            sources=sources,
            port_exit_apertures=port_exit_apertures,
            source_freq_max=source_freq_max,
            source_mesh_valid=source_mesh_valid,
            source_aperture_valid=source_aperture_valid,
            frame=frame,
        )
        manifest["status"] = "complete"
        manifest["current_phase"] = "complete"
        manifest.pop("current_source", None)
        returncode = 0
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        returncode = 1
    finally:
        if lock_file is not None:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(manifest_path, manifest)

    print(json.dumps(_jsonable(manifest), indent=2, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
