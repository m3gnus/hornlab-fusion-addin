#!/usr/bin/env python3
"""Solve a Fusion-exported WG Metal mesh directly with hornlab-metal-bem.

This script is intentionally independent of Waveguide Generator. It consumes
the tagged multi-source mesh from ``prepare_step_for_wg_metal.py`` and solves
one source at a time with an explicit observation frame using the canonical
``hornlab_metal_bem`` native Metal solver (dense Accelerate ``cgesv`` solve,
no iterative fallback). Sources are driven at unit normal ACCELERATION (the
``SolveConfig`` default), so per-driver levels are arbitrary-scale; only the
coupled driver-LEM path converts a basis to absolute voltage-driven pressure
(see ``_voltage_drive_pressure``).

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
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np


LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = REPO_ROOT / "scripts" / "render_run_report.py"
# Workspace checkouts win over installed packages when present. The HornLab
# helper packages may be checked out as top-level siblings or inside a sibling
# HornLab repo; elsewhere imports resolve from the active environment.
_WORKSPACE_PACKAGE_CANDIDATES = (
    REPO_ROOT / "fusion-addins" / "WGMetalPipeline",
    REPO_ROOT.parent / "hornlab-metal-bem",
    REPO_ROOT.parent / "hornlab-plots",
    REPO_ROOT.parent / "hornlab-sim",
    REPO_ROOT.parent / "HornLab" / "hornlab-metal-bem",
    REPO_ROOT.parent / "HornLab" / "hornlab-plots",
    REPO_ROOT.parent / "HornLab" / "hornlab-sim",
)
for package_dir in reversed(_WORKSPACE_PACKAGE_CANDIDATES):
    if package_dir.is_dir() and str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))

from hornlab_sim.methods import bandpass, driver_coupling, radiation_impedance  # noqa: E402
from fusion_pipeline_launch import (  # noqa: E402
    DriverLemParseError,
    DriverLemSpec,
    parse_driver_lem_cli_entries,
)
from hornlab_plots import (  # noqa: E402
    FrequencyResponseCurve,
    save_beamwidth_plot,
    save_directivity_plot,
    save_directivity_power_plot,
    save_excursion_plot,
    save_frequency_response_plot as _save_frequency_response_plot,
    save_group_delay_plot,
    save_impedance_plot,
    save_interference_heatmap,
    set_theme,
)
from hornlab_metal_bem import (  # noqa: E402
    ObservationConfig,
    ObservationFrame,
    SolveConfig,
    solve,
    solve_multi_source,
)

# Recorded in manifests: where the helper packages actually resolved from.
HORNLAB_SIM_DIR = Path(sys.modules["hornlab_sim"].__file__).resolve().parent
HORNLAB_PLOTS_DIR = Path(sys.modules["hornlab_plots"].__file__).resolve().parent
METAL_BEM_DIR = Path(sys.modules["hornlab_metal_bem"].__file__).resolve().parent


P_REF = 2.0e-5
SPEED_OF_SOUND_M_S = 343.0
CANONICAL_SOLVE_SOURCE_PRIORITY = {
    "HF": 0,
    "MF": 1,
    "LF": 2,
}
DEFAULT_DIRECT_SOLVE_LOCK_PATH = Path("/tmp/hornlab-fusion-direct-solve.lock")
RUN_MANIFESTS_DIR_NAME = "manifests"
SOLVER_LAYOUT_VERSION = 2


class SolverOutputLayout:
    def __init__(self, root: Path, *, layout_version: int) -> None:
        self.root = root
        self.layout_version = int(layout_version)
        if self.layout_version >= 2:
            self.sources_dir = root / "sources"
            self.combined_dir = root / "combined"
            self.cardioid_dir = root / "cardioid"
            self.driver_lem_dir = root / "driver-lem"
            self.derived_dir = root / "derived"
        else:
            self.sources_dir = root
            self.combined_dir = root
            self.cardioid_dir = root
            self.driver_lem_dir = root
            self.derived_dir = root
        self.vituixcad_dir = root / "vituixcad"
        self.logs_dir = root / "logs"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (
            self.sources_dir,
            self.combined_dir,
            self.cardioid_dir,
            self.driver_lem_dir,
            self.derived_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "version": self.layout_version,
            "sources_dir": str(self.sources_dir),
            "combined_dir": str(self.combined_dir),
            "cardioid_dir": str(self.cardioid_dir),
            "driver_lem_dir": str(self.driver_lem_dir),
            "derived_dir": str(self.derived_dir),
            "vituixcad_dir": str(self.vituixcad_dir),
            "logs_dir": str(self.logs_dir),
        }


def _layout_version_for_run(
    *,
    postprocess_only: bool,
    previous_direct_manifest: dict[str, Any],
    previous_final_manifest: dict[str, Any],
) -> int:
    if not postprocess_only:
        return SOLVER_LAYOUT_VERSION
    candidates = [
        previous_direct_manifest.get("layout_version"),
        previous_final_manifest.get("direct_solve", {}).get("layout_version"),
    ]
    for raw in candidates:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 2:
            return SOLVER_LAYOUT_VERSION
    return 1


def _render_run_report(out_dir: Path) -> dict[str, Any] | None:
    if not REPORT_SCRIPT.exists():
        return None
    cmd = [sys.executable, str(REPORT_SCRIPT), str(out_dir)]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), text=True, check=False)
    return {
        "path": str(out_dir / "report.html"),
        "returncode": int(result.returncode),
        "status": "complete" if result.returncode == 0 else "failed",
        "command": cmd,
    }


def _coerce_phase_overlay_curve(curve: Any) -> tuple[np.ndarray, np.ndarray, str, str, bool]:
    if isinstance(curve, dict):
        freqs = curve.get("frequencies", curve.get("freqs"))
        phase = curve.get("phase_deg", curve.get("phase"))
        label = str(curve.get("label", curve.get("role", "phase")))
        role = str(curve.get("role", "other"))
        crossover = bool(curve.get("crossover", False))
    elif isinstance(curve, (list, tuple)) and len(curve) >= 2:
        freqs = curve[0]
        phase = curve[1]
        label = str(curve[2]) if len(curve) > 2 else "phase"
        role = str(curve[3]) if len(curve) > 3 else "other"
        crossover = bool(curve[4]) if len(curve) > 4 else False
    else:
        raise TypeError("phase overlay curves must be dicts or tuples")
    return (
        np.asarray(freqs, dtype=np.float64),
        np.asarray(phase, dtype=np.float64),
        label,
        role,
        crossover,
    )


def save_frequency_response_plot(
    output_path: Path,
    curves: list[FrequencyResponseCurve],
    dpi: int | None = None,
    *,
    phase_curves: list[Any] | None = None,
    **kwargs: Any,
) -> Path | None:
    """Save the canonical response plot, optionally with wrapped phase overlays."""
    if not phase_curves:
        if dpi is None:
            return _save_frequency_response_plot(output_path, curves, **kwargs)
        return _save_frequency_response_plot(output_path, curves, dpi=dpi, **kwargs)

    from hornlab_plots.charts import (  # noqa: PLC0415
        _build_frequency_response_figure,
        _response_curve_style,
    )
    from hornlab_plots.style import apply_theme_overrides  # noqa: PLC0415

    theme_obj = apply_theme_overrides(
        kwargs.get("theme"),
        colors=kwargs.get("colors"),
        line_colors=kwargs.get("line_colors"),
        response_colors=kwargs.get("response_colors"),
    )
    fig = _build_frequency_response_figure(curves, **kwargs)
    if fig is None:
        return None
    ax = fig.axes[0]
    phase_ax = ax.twinx()
    phase_ax.set_ylabel("Phase [deg]", color=theme_obj.text_color, fontsize=10)
    phase_ax.set_ylim(-180.0, 180.0)
    phase_ax.set_yticks([-180.0, -90.0, 0.0, 90.0, 180.0])
    phase_ax.tick_params(colors=theme_obj.tick_color, labelsize=8)
    phase_ax.set_facecolor(theme_obj.axes_bg)
    phase_ax.spines["right"].set_color(theme_obj.spine_color)
    for spine_name in ("left", "top", "bottom"):
        phase_ax.spines[spine_name].set_visible(False)

    for raw_curve in phase_curves:
        freqs, phase_deg, label, role, crossover = _coerce_phase_overlay_curve(raw_curve)
        if freqs.size == 0 or phase_deg.size == 0:
            continue
        n = min(freqs.size, phase_deg.size)
        freqs = freqs[:n]
        phase_deg = phase_deg[:n]
        finite = np.isfinite(freqs) & np.isfinite(phase_deg) & (freqs > 0.0)
        if not np.any(finite):
            continue
        style = _response_curve_style(
            FrequencyResponseCurve(
                frequencies=freqs,
                spl_db=np.zeros_like(freqs),
                label=label,
                role=role,
                crossover=crossover,
            ),
            theme=theme_obj,
        )
        phase_ax.semilogx(
            freqs[finite],
            phase_deg[finite],
            color=style["color"],
            linewidth=1.0,
            linestyle="-." if crossover else "--",
            alpha=0.36,
            zorder=1,
        )
    phase_ax.text(
        0.995,
        0.02,
        "dashed: display-flattened phase",
        transform=phase_ax.transAxes,
        ha="right",
        va="bottom",
        color=theme_obj.text_color,
        fontsize=8,
        alpha=0.68,
    )
    fig.tight_layout(pad=1.5)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        str(out),
        format="png",
        dpi=150 if dpi is None else dpi,
        facecolor=fig.get_facecolor(),
        edgecolor="none",
        bbox_inches="tight",
    )
    import matplotlib.pyplot as plt  # noqa: PLC0415

    plt.close(fig)
    return out


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
        source_normalization: str = "unit_normal_acceleration",
        surface_pressure_avg_solver: np.ndarray | None = None,
        source_area_m2: float | None = None,
        source_motion: str = "normal",
    ) -> None:
        self.source_name = source_name
        self.source_tag = source_tag
        self.frequencies_hz = frequencies_hz
        self.observation_angles_deg = observation_angles_deg
        self.observation_planes = observation_planes
        self.pressure_complex = pressure_complex
        self.source_normalization = source_normalization
        self.surface_pressure_avg_solver = surface_pressure_avg_solver
        self.source_area_m2 = source_area_m2
        self.source_motion = str(source_motion or "normal")


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
    path.parent.mkdir(parents=True, exist_ok=True)
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
SURFACE_PRESSURE_AVG_PHASE_CONVENTION = "solver_exp_minus_jwt"


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


def _surface_pressure_avg_for_tag(result, source_tag: int) -> np.ndarray | None:
    surface = getattr(result, "surface_pressure_avg", None) or {}
    if source_tag in surface:
        return np.asarray(surface[source_tag], dtype=np.complex128)
    key = str(source_tag)
    if key in surface:
        return np.asarray(surface[key], dtype=np.complex128)
    return None


def _write_pressure_basis_npz(
    path: Path,
    result,
    *,
    source_name: str,
    source_tag: int,
    source_area_m2: float | None = None,
    source_motion: str = "normal",
) -> None:
    arrays: dict[str, Any] = {
        "source_name": np.asarray(source_name),
        "source_tag": np.asarray(source_tag, dtype=np.int32),
        "frequencies_hz": np.asarray(result.frequencies_hz, dtype=np.float64),
        "observation_angles_deg": np.asarray(
            result.observation_angles_deg,
            dtype=np.float64,
        ),
        "observation_planes": np.asarray(result.observation_planes, dtype=str),
        "pressure_complex": np.conjugate(
            np.asarray(result.pressure_complex, dtype=np.complex128)
        ),
        "phase_convention": np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
        "source_normalization": np.asarray("unit_normal_acceleration"),
    }
    if str(source_motion) != "normal":
        arrays["source_motion"] = np.asarray(str(source_motion))
    surface_pressure_avg = _surface_pressure_avg_for_tag(result, source_tag)
    if surface_pressure_avg is not None:
        arrays["surface_pressure_avg_solver"] = surface_pressure_avg
        arrays["surface_pressure_avg_phase_convention"] = np.asarray(
            SURFACE_PRESSURE_AVG_PHASE_CONVENTION
        )
    if source_area_m2 is not None:
        arrays["source_area_m2"] = np.asarray(float(source_area_m2), dtype=np.float64)
    np.savez_compressed(path, **arrays)


def _pressure_basis_from_result(
    result,
    *,
    source_name: str,
    source_tag: int,
    source_area_m2: float | None = None,
    source_motion: str = "normal",
) -> PressureBasis:
    return PressureBasis(
        source_name=source_name,
        source_tag=source_tag,
        frequencies_hz=np.asarray(result.frequencies_hz, dtype=np.float64),
        observation_angles_deg=np.asarray(
            result.observation_angles_deg,
            dtype=np.float64,
        ),
        observation_planes=np.asarray(result.observation_planes, dtype=str),
        pressure_complex=np.conjugate(
            np.asarray(result.pressure_complex, dtype=np.complex128)
        ),
        source_normalization="unit_normal_acceleration",
        surface_pressure_avg_solver=_surface_pressure_avg_for_tag(result, source_tag),
        source_area_m2=source_area_m2,
        source_motion=source_motion,
    )


def _write_active_pressure_npz(
    path: Path,
    basis: PressureBasis,
    pressure_complex: np.ndarray,
    *,
    source_normalization: str,
    source_area_m2: float | None = None,
) -> None:
    arrays: dict[str, Any] = {
        "source_name": np.asarray(basis.source_name),
        "source_tag": np.asarray(basis.source_tag, dtype=np.int32),
        "frequencies_hz": np.asarray(basis.frequencies_hz, dtype=np.float64),
        "observation_angles_deg": np.asarray(
            basis.observation_angles_deg,
            dtype=np.float64,
        ),
        "observation_planes": np.asarray(basis.observation_planes, dtype=str),
        "pressure_complex": np.asarray(pressure_complex, dtype=np.complex128),
        "phase_convention": np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
        "source_normalization": np.asarray(source_normalization),
    }
    area = source_area_m2 if source_area_m2 is not None else basis.source_area_m2
    if area is not None:
        arrays["source_area_m2"] = np.asarray(float(area), dtype=np.float64)
    if str(basis.source_motion) != "normal":
        arrays["source_motion"] = np.asarray(str(basis.source_motion))
    np.savez_compressed(path, **arrays)


def _active_pressure_basis(
    basis: PressureBasis,
    pressure_complex: np.ndarray,
    *,
    source_normalization: str,
    source_area_m2: float | None = None,
) -> PressureBasis:
    return PressureBasis(
        source_name=basis.source_name,
        source_tag=basis.source_tag,
        frequencies_hz=basis.frequencies_hz,
        observation_angles_deg=basis.observation_angles_deg,
        observation_planes=basis.observation_planes,
        pressure_complex=np.asarray(pressure_complex, dtype=np.complex128),
        source_normalization=source_normalization,
        surface_pressure_avg_solver=basis.surface_pressure_avg_solver,
        source_area_m2=source_area_m2 if source_area_m2 is not None else basis.source_area_m2,
        source_motion=basis.source_motion,
    )


def _load_pressure_basis(path: Path) -> PressureBasis:
    with np.load(path, allow_pickle=False) as data:
        source_normalization = "unit_normal_acceleration"
        if "source_normalization" in data:
            source_normalization = str(np.asarray(data["source_normalization"]).item())
        surface_pressure_avg = None
        if "surface_pressure_avg_solver" in data:
            raw_surface_convention = (
                data["surface_pressure_avg_phase_convention"]
                if "surface_pressure_avg_phase_convention" in data
                else np.asarray(SURFACE_PRESSURE_AVG_PHASE_CONVENTION)
            )
            surface_convention = str(
                np.asarray(raw_surface_convention).item()
            )
            if surface_convention != SURFACE_PRESSURE_AVG_PHASE_CONVENTION:
                raise ValueError(
                    f"{path} stores surface_pressure_avg_solver with unsupported "
                    f"phase convention {surface_convention!r}; expected "
                    f"{SURFACE_PRESSURE_AVG_PHASE_CONVENTION!r}"
                )
            surface_pressure_avg = np.asarray(
                data["surface_pressure_avg_solver"],
                dtype=np.complex128,
            )
        source_area_m2 = None
        if "source_area_m2" in data:
            source_area_m2 = float(np.asarray(data["source_area_m2"]).item())
        source_motion = "normal"
        if "source_motion" in data:
            source_motion = str(np.asarray(data["source_motion"]).item())
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
            source_normalization=source_normalization,
            surface_pressure_avg_solver=surface_pressure_avg,
            source_area_m2=source_area_m2,
            source_motion=source_motion,
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


def _ratio_group_delay_s(
    freqs: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    target_hz: float,
    *,
    rel_window: float = 0.15,
) -> float | None:
    """Local group delay of ``lower/upper`` at ``target_hz`` in the ``e^{-j w
    tau}`` (delay-to-apply-to-lower) sense: ``d/dw arg(lower/upper)``.

    Estimated by a linear fit of the unwrapped ratio phase against angular
    frequency over ``target_hz * (1 +/- rel_window)`` (falling back to the
    nearest few master-grid samples). For two pure-arrival radiators this
    recovers the physical arrival offset ``t_upper - t_lower``; here it is used
    only to pick the correct period branch of the phase-value alignment, so a
    rough slope is enough. Returns ``None`` when it cannot be estimated.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    ratio = np.asarray(lower, dtype=np.complex128) / np.asarray(
        upper, dtype=np.complex128
    )
    if not np.all(np.isfinite(ratio)):
        return None
    window = (freqs >= target_hz * (1.0 - rel_window)) & (
        freqs <= target_hz * (1.0 + rel_window)
    )
    if np.count_nonzero(window) < 3:
        centre = int(np.argmin(np.abs(freqs - target_hz)))
        lo_i = max(0, centre - 2)
        hi_i = min(freqs.size, centre + 3)
        sel = np.zeros(freqs.size, dtype=bool)
        sel[lo_i:hi_i] = True
        window = sel
    f_sel = freqs[window]
    if f_sel.size < 2:
        return None
    phase = np.unwrap(np.angle(ratio))[window]
    omega = 2.0 * np.pi * f_sel
    slope = float(np.polyfit(omega, phase, 1)[0])  # d(arg)/d(omega)
    if not np.isfinite(slope):
        return None
    return slope


def _phase_equivalent_delay_s(
    phase_diff_rad: float,
    freq_hz: float,
    *,
    group_delay_hint_s: float | None = None,
) -> float:
    period_s = 1.0 / float(freq_hz)
    # np.angle returns the principal value in (-pi, pi], so this principal
    # delay already lies in (-T/2, T/2] and is exactly phase-coherent at fc.
    principal_s = float(phase_diff_rad) / (2.0 * np.pi * float(freq_hz))
    if group_delay_hint_s is None or not np.isfinite(group_delay_hint_s):
        # No physical disambiguation: pick the smallest |delay| branch (nearest
        # zero relative delay). Correct whenever the true inter-driver arrival
        # gap is under half a period at the crossover.
        return (principal_s + 0.5 * period_s) % period_s - 0.5 * period_s
    # The phase at fc fixes the delay only modulo one period. Choose the period
    # branch nearest the measured group delay of the (unfiltered) driver ratio
    # -- the physical arrival offset -- so the sum is coherent at fc AND on the
    # correct cycle even when that offset exceeds half a period (a high MF/HF
    # crossover with a real tap-to-throat path difference). The old
    # minimum-non-negative wrap ignored this and turned a small negative phase
    # (e.g. a passive-cardioid port pulling MF a few degrees past LF) into a
    # near-full-period delay that cancelled hard just off fc. Downstream global
    # normalization (delays_s -= min) restores non-negative, realizable delays.
    n = round((float(group_delay_hint_s) - principal_s) / period_s)
    return principal_s + n * period_s


def _crossover_chain(
    present: list[str],
    *,
    lf_mf_hz: float | None,
    mf_hf_hz: float | None,
    lf_hf_hz: float | None = None,
) -> tuple[list[str], list[float]] | tuple[None, str]:
    """Pick the ordered driver chain and its crossover frequencies.

    Three drivers need both the LF/MF and MF/HF crossover fields. Two drivers
    take the pair's natural field: LF+MF -> LF/MF, MF+HF -> MF/HF, and the
    non-adjacent LF+HF two-way (MF absent) -> the dedicated LF/HF field, which
    overrides any leftover LF/MF or MF/HF value. A pair whose natural field is
    empty falls back to the single filled field; an LF+HF pair with no LF/HF
    field but both LF/MF and MF/HF filled stays ambiguous (the tool refuses to
    guess which one bridges LF->HF). Returns ``(members, crossovers_hz)`` or
    ``(None, reason)``.
    """
    members = [name for name in ("LF", "MF", "HF") if name in present]
    if len(members) < 2:
        return None, "need at least two of LF/MF/HF pressure bases"
    if len(members) == 3:
        if lf_mf_hz is None or mf_hf_hz is None:
            return None, "three-way sum needs both LF/MF and MF/HF crossover frequencies"
        return members, [float(lf_mf_hz), float(mf_hf_hz)]
    pair = (members[0], members[1])
    natural = {
        ("LF", "MF"): lf_mf_hz,
        ("MF", "HF"): mf_hf_hz,
        ("LF", "HF"): lf_hf_hz,
    }.get(pair)
    if natural is not None:
        return members, [float(natural)]
    provided = [value for value in (lf_mf_hz, mf_hf_hz, lf_hf_hz) if value is not None]
    if len(provided) == 1:
        return members, [float(provided[0])]
    if not provided:
        return None, "no crossover frequency provided"
    if pair == ("LF", "HF"):
        return None, (
            "ambiguous crossover for the LF/HF two-way: set the LF/HF crossover "
            "field, or clear one of LF/MF and MF/HF so exactly one crossover "
            "frequency remains"
        )
    return None, (
        f"ambiguous crossover for the {pair[0]}/{pair[1]} pair: "
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
        "- Chooses the minimum-magnitude phase-equivalent delay per crossover (branch nearest zero relative delay), then normalizes the chain to non-negative delays.",
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
        phase_curves=[
            (
                freqs,
                _phase_deg_from_pressure(
                    combined_grid[:, plane_index, index],
                    frequencies_hz=freqs,
                    polar_distance_m=polar_distance_m,
                    impulse_aligned=True,
                ),
                f"{angles[index]:g} deg",
                _OFF_AXIS_CURVE_ROLES[min(order, len(_OFF_AXIS_CURVE_ROLES) - 1)],
            )
            for order, index in enumerate(picked)
        ],
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
    no relative change). Levels are unit-source-drive SPL (unit normal
    acceleration; arbitrary per-driver scale) except for a coupled
    MF_cardioid export, which is voltage-driven. No ZMA is exported
    for direct BEM drivers; coupled MF_cardioid carries its calculated ZMA.
    """
    by_name = {str(result["name"]).strip().upper(): result for result in source_results}
    export_bases: list[tuple[str, PressureBasis]] = [
        (name, _source_active_basis(by_name[name]))
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
    copied_zmas: dict[str, Path] = {}
    for source_result in source_results:
        driver_payload = source_result.get("driver_lem")
        if not isinstance(driver_payload, dict) or driver_payload.get("status") != "complete":
            continue
        outputs = driver_payload.get("outputs", {})
        if isinstance(outputs, dict) and outputs.get("impedance_zma"):
            source_zma = Path(outputs["impedance_zma"])
            if not source_zma.exists():
                continue
            export_dir.mkdir(parents=True, exist_ok=True)
            copied_zma = export_dir / source_zma.name
            shutil.copy2(source_zma, copied_zma)
            copied_zmas[str(source_result["name"])] = copied_zma
            continue
        private_impedance = source_result.get("_driver_lem_impedance")
        if isinstance(private_impedance, dict):
            try:
                freqs = np.asarray(private_impedance["frequencies_hz"], dtype=np.float64)
                impedance = np.asarray(
                    private_impedance["impedance_ohm"],
                    dtype=np.complex128,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if freqs.shape != impedance.shape:
                continue
            export_dir.mkdir(parents=True, exist_ok=True)
            copied_zma = export_dir / f"{_safe_stem(source_result['name'])}_impedance.zma"
            _write_zma(
                copied_zma,
                freqs,
                impedance,
                comment=(
                    f"{source_result['name']} driver LEM electrical input "
                    "impedance exported for VituixCAD"
                ),
            )
            copied_zmas[str(source_result["name"])] = copied_zma
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
                    copied_zmas["MF_cardioid"] = copied_zma
    files_written = 0
    for name, basis in export_bases:
        freqs = np.asarray(basis.frequencies_hz, dtype=np.float64)
        angles = np.asarray(basis.observation_angles_deg, dtype=np.float64)
        level_note = (
            "voltage-driven Driver LEM"
            if basis.source_normalization == "voltage_driven_driver_lem"
            else "unit source drive"
        )
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
                            f"{level_note}, common ToF {polar_distance_m:g} m "
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
    if copied_zmas:
        zma_lines = [
            "- Coupled driver ZMA files are included:",
            *[
                f"  {name}: {path.name}"
                for name, path in sorted(copied_zmas.items())
            ],
            "- Drivers without Driver LEM specs remain unit-source FRDs and",
            "  need measured or datasheet impedance for passive crossover work.",
        ]
    else:
        zma_lines = [
            "- No ZMA is exported: no voltage-driven Driver LEM specs were",
            "  applied. For passive crossover work import measured or",
            "  datasheet impedance per driver.",
        ]
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
                "- Driver LEM sources are voltage-driven at the run drive",
                "  voltage; uncoupled sources are unit-source-drive SPL and",
                "  still need manual scaling.",
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
    if copied_zmas:
        payload["outputs"]["vituixcad_driver_zmas"] = {
            name: str(path) for name, path in sorted(copied_zmas.items())
        }
        if "MF_cardioid" in copied_zmas:
            payload["outputs"]["vituixcad_mf_cardioid_zma"] = str(
                copied_zmas["MF_cardioid"]
            )
    if active_vxp is not None:
        payload["active_crossover_project"] = {
            "type": "active_lr4_vxp",
            "path": str(active_vxp),
        }
        payload["outputs"]["vituixcad_active_lr4_vxp"] = str(active_vxp)
    return payload


_DEFAULT_DERIVED_DIR = object()


def _write_crossover_alignment_outputs(
    out_dir: Path,
    source_results: list[dict[str, Any]],
    *,
    lf_mf_hz: float | None,
    mf_hf_hz: float | None,
    lf_hf_hz: float | None = None,
    polar_distance_m: float,
    mesh_valid_hz: float | None,
    mesh_valid_radiating_hz: float | None,
    mf_override_npz: Path | None = None,
    mf_override_kind: str = "direct",
    derived_dir: Path | None | object = _DEFAULT_DERIVED_DIR,
) -> dict[str, Any] | None:
    if lf_mf_hz is None and mf_hf_hz is None and lf_hf_hz is None:
        return None
    by_name = {str(result["name"]).strip().upper(): result for result in source_results}
    chain, chain_or_reason = _crossover_chain(
        [name for name in ("LF", "MF", "HF") if name in by_name],
        lf_mf_hz=lf_mf_hz,
        mf_hf_hz=mf_hf_hz,
        lf_hf_hz=lf_hf_hz,
    )
    if chain is None:
        return {"status": "skipped", "reason": str(chain_or_reason)}
    members, crossovers_hz = chain, list(chain_or_reason)

    bases = {name: _source_active_basis(by_name[name]) for name in members}
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
    # crossover contributes the minimum-magnitude phase-equivalent delay
    # between its adjacent pair (see _phase_equivalent_delay_s), and the chain
    # is normalized to non-negative delays afterward. A crossover above a
    # clamped member's solved top would read the zero-filled region (phase 0 ->
    # bogus delay), so the pair phase is measured at the highest frequency both
    # members solved.
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
        # Disambiguate the period branch with the physical arrival offset,
        # measured as the group delay of the *unfiltered* driver ratio near the
        # crossover (the LR4 filter phases cancel at fc but their group delays
        # do not, so the raw pressures give the cleaner arrival estimate).
        group_delay_hint_s = _ratio_group_delay_s(
            freqs, pressures[lower], pressures[upper], eval_hz
        )
        pair_delay = _phase_equivalent_delay_s(
            np.angle(lower_at_xo / upper_at_xo),
            eval_hz,
            group_delay_hint_s=group_delay_hint_s,
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
        phase_curves=[
            (
                freqs,
                _phase_deg_from_pressure(
                    aligned[name],
                    frequencies_hz=freqs,
                    polar_distance_m=polar_distance_m,
                    impulse_aligned=True,
                    fit_frequency_range_hz=_member_phase_fit_band_hz(
                        freqs,
                        index,
                        crossovers_hz,
                    ),
                ),
                _member_filter_label(
                    name, index, members, crossovers_hz, level_gains_db[name]
                ),
                member_roles.get(name, "other"),
                True,
            )
            for index, name in enumerate(members)
        ]
        + [
            (
                freqs,
                _phase_deg_from_pressure(
                    combined_unaligned,
                    frequencies_hz=freqs,
                    polar_distance_m=polar_distance_m,
                    impulse_aligned=True,
                ),
                "Combined before delay",
                "raw",
            ),
            (
                freqs,
                _phase_deg_from_pressure(
                    combined_aligned,
                    frequencies_hz=freqs,
                    polar_distance_m=polar_distance_m,
                    impulse_aligned=True,
                ),
                "Combined time aligned",
                "combined",
            ),
        ],
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

    derived_outputs = None
    derived_output_dir = out_dir if derived_dir is _DEFAULT_DERIVED_DIR else derived_dir
    if derived_output_dir is not None:
        derived_outputs = _write_pressure_grid_derived_artifacts(
            Path(derived_output_dir),
            "combined_time_aligned",
            label="Combined time-aligned sum",
            frequencies_hz=freqs,
            angles_deg=angles_deg,
            planes=planes,
            pressure_complex=combined_pressure_grid,
            polar_distance_m=polar_distance_m,
            mesh_valid_hz=mesh_valid_hz,
            mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        )

    interference_png = out_dir / "combined_interference_heatmap_time_aligned.png"
    save_interference_heatmap(
        interference_png,
        freqs,
        aligned_grids,
        angles_deg,
        planes,
        members=members,
        crossovers_hz=crossovers_hz,
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
    payload = {
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
    if derived_outputs is not None:
        payload["outputs"].update(
            {
                "combined_time_aligned_directivity_power_png": derived_outputs[
                    "directivity_power_png"
                ],
                "combined_time_aligned_directivity_power_csv": derived_outputs[
                    "directivity_power_csv"
                ],
                "combined_time_aligned_directivity_power_json": derived_outputs[
                    "directivity_power_json"
                ],
                "combined_time_aligned_beamwidth_png": derived_outputs["beamwidth_png"],
                "combined_time_aligned_beamwidth_csv": derived_outputs["beamwidth_csv"],
                "combined_time_aligned_beamwidth_json": derived_outputs["beamwidth_json"],
                "combined_time_aligned_group_delay_png": derived_outputs[
                    "group_delay_png"
                ],
                "combined_time_aligned_group_delay_csv": derived_outputs[
                    "group_delay_csv"
                ],
                "combined_time_aligned_group_delay_json": derived_outputs[
                    "group_delay_json"
                ],
            }
        )
    return payload


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


POLAR_POWER_APPROXIMATION_NOTE = (
    "Polar-cut estimate: intensity is averaged over the stored planes at each "
    "polar angle, solid-angle weighted, and extrapolated to 4*pi; this is not "
    "a full-sphere integration."
)


def _write_csv(path: Path, header: list[str], rows: list[list[float | str | None]]) -> None:
    def _fmt(value: float | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        number = float(value)
        if not np.isfinite(number):
            return ""
        return f"{number:.10g}"

    path.write_text(
        ",".join(header)
        + "\n"
        + "\n".join(",".join(_fmt(value) for value in row) for row in rows)
        + "\n",
        encoding="utf-8",
    )


def _solid_angle_weights_for_polar_angles(angles_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    theta = np.radians(np.abs(np.asarray(angles_deg, dtype=np.float64)))
    theta = theta[np.isfinite(theta)]
    theta = theta[(theta >= 0.0) & (theta <= np.pi)]
    if theta.size == 0:
        raise ValueError("polar angle grid has no finite angles in 0..180 deg")
    theta = np.asarray(sorted({round(float(value), 12) for value in theta}), dtype=np.float64)
    if theta.size == 1:
        half_width = 0.5 * np.pi
        lower = np.asarray([max(0.0, float(theta[0]) - half_width)])
        upper = np.asarray([min(np.pi, float(theta[0]) + half_width)])
    else:
        edges = np.empty(theta.size + 1, dtype=np.float64)
        edges[1:-1] = 0.5 * (theta[:-1] + theta[1:])
        edges[0] = max(0.0, theta[0] - 0.5 * (theta[1] - theta[0]))
        edges[-1] = min(np.pi, theta[-1] + 0.5 * (theta[-1] - theta[-2]))
        lower = edges[:-1]
        upper = edges[1:]
    weights = 2.0 * np.pi * (np.cos(lower) - np.cos(upper))
    return theta, np.maximum(weights, 0.0)


def _plane_averaged_intensity_by_polar_angle(
    pressure_complex: np.ndarray,
    angles_deg: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    pressure = np.asarray(pressure_complex, dtype=np.complex128)
    angles = np.asarray(angles_deg, dtype=np.float64)
    if pressure.ndim != 3:
        raise ValueError(f"pressure grid must be (freq, plane, angle), got {pressure.shape}")
    if pressure.shape[2] != angles.size:
        raise ValueError("pressure angle dimension does not match observation angles")
    theta_all = np.radians(np.abs(angles))
    unique_theta, _weights = _solid_angle_weights_for_polar_angles(angles)
    intensity = np.abs(pressure) ** 2 / (
        float(radiation_impedance.RHO_AIR) * float(radiation_impedance.C_AIR)
    )
    grouped = np.zeros((pressure.shape[0], unique_theta.size), dtype=np.float64)
    for index, theta in enumerate(unique_theta):
        mask = np.isclose(theta_all, theta, rtol=0.0, atol=1.0e-11)
        grouped[:, index] = np.mean(intensity[:, :, mask], axis=(1, 2))
    return unique_theta, grouped


def _directivity_power_metrics_from_pressure(
    pressure_complex: np.ndarray,
    angles_deg: np.ndarray,
    *,
    polar_distance_m: float,
) -> dict[str, Any]:
    theta, weights = _solid_angle_weights_for_polar_angles(angles_deg)
    intensity_theta, intensity = _plane_averaged_intensity_by_polar_angle(
        pressure_complex,
        angles_deg,
    )
    if intensity_theta.shape != theta.shape or not np.allclose(
        intensity_theta,
        theta,
        rtol=0.0,
        atol=1.0e-10,
    ):
        raise ValueError("polar angle grouping mismatch while computing power response")
    weight_sum = float(np.sum(weights))
    if weight_sum <= 0.0:
        raise ValueError("polar solid-angle weights sum to zero")
    weighted_intensity = intensity @ weights
    spatial_average_intensity = weighted_intensity / weight_sum
    on_axis_index = int(np.argmin(theta))
    on_axis_intensity = intensity[:, on_axis_index]
    intensity_ref = P_REF**2 / (
        float(radiation_impedance.RHO_AIR) * float(radiation_impedance.C_AIR)
    )
    floor = intensity_ref * 1.0e-30
    directivity_index_db = 10.0 * np.log10(
        np.maximum(on_axis_intensity, floor)
        / np.maximum(spatial_average_intensity, floor)
    )
    power_response_db = 10.0 * np.log10(
        np.maximum(spatial_average_intensity, floor) / intensity_ref
    )
    on_axis_spl_db = 10.0 * np.log10(
        np.maximum(on_axis_intensity, floor) / intensity_ref
    )
    acoustic_power_w = (
        4.0
        * np.pi
        * float(polar_distance_m) ** 2
        * spatial_average_intensity
    )
    return {
        "directivity_index_db": directivity_index_db,
        "power_response_db": power_response_db,
        "on_axis_spl_db": on_axis_spl_db,
        "acoustic_power_w": acoustic_power_w,
        "spatial_average_intensity_w_m2": spatial_average_intensity,
        "solid_angle_sum_sr": weight_sum,
        "solid_angle_coverage_fraction": weight_sum / (4.0 * np.pi),
        "polar_angles_deg": np.degrees(theta),
        "polar_weights_sr": weights,
        "approximation": POLAR_POWER_APPROXIMATION_NOTE,
    }


def _estimate_impulse_peak_delay_s(
    frequencies_hz: np.ndarray,
    pressure: np.ndarray,
    *,
    polar_distance_m: float | None = None,
    fit_frequency_range_hz: tuple[float | None, float | None] | None = None,
) -> float:
    """Estimate the bulk delay whose removal aligns phase at the impulse peak.

    The solve grid is usually sparse/log-spaced, so this intentionally uses a
    weighted linear phase fit rather than synthesizing an FFT impulse. When a
    fit band is supplied, only that operating band controls the displayed
    delay; this keeps LF/MF/HF phase overlays visually flat where the driver is
    actually used.
    """
    freqs = np.asarray(frequencies_hz, dtype=np.float64).reshape(-1)
    values = np.asarray(pressure, dtype=np.complex128).reshape(-1)
    common_delay_s = (
        float(polar_distance_m) / SPEED_OF_SOUND_M_S
        if polar_distance_m is not None
        else 0.0
    )
    n = min(freqs.size, values.size)
    if n < 2:
        return common_delay_s
    freqs = freqs[:n]
    values = values[:n]
    omega = 2.0 * np.pi * freqs
    magnitudes = np.abs(values)
    finite = (
        np.isfinite(freqs)
        & np.isfinite(values.real)
        & np.isfinite(values.imag)
        & np.isfinite(magnitudes)
        & (freqs > 0.0)
    )
    if not np.any(finite):
        return common_delay_s
    max_mag = float(np.max(magnitudes[finite]))
    if max_mag <= 0.0:
        return common_delay_s
    finite &= magnitudes >= max_mag * 1.0e-8
    if fit_frequency_range_hz is not None:
        lo_hz, hi_hz = fit_frequency_range_hz
        band = finite.copy()
        if lo_hz is not None:
            band &= freqs >= float(lo_hz)
        if hi_hz is not None:
            band &= freqs <= float(hi_hz)
        if np.count_nonzero(band) >= 2:
            finite = band
    if np.count_nonzero(finite) < 2:
        return common_delay_s
    omega = omega[finite]
    values = values[finite]
    magnitudes = magnitudes[finite]
    residual = values * np.exp(1j * omega * common_delay_s)
    phase = np.unwrap(np.angle(residual))
    weights = np.sqrt(np.maximum(magnitudes / max_mag, 1.0e-8))
    weight_sum = float(np.sum(weights))
    if weight_sum <= 0.0:
        return common_delay_s
    weights = weights / weight_sum
    omega_center = float(np.sum(weights * omega))
    phase_center = float(np.sum(weights * phase))
    omega_delta = omega - omega_center
    denominator = float(np.sum(weights * omega_delta * omega_delta))
    if denominator <= 0.0:
        return common_delay_s
    slope = float(np.sum(weights * omega_delta * (phase - phase_center)) / denominator)
    delay = common_delay_s - slope
    return delay if math.isfinite(delay) else common_delay_s


def _phase_deg_from_pressure(
    pressure: np.ndarray,
    *,
    frequencies_hz: np.ndarray | None = None,
    polar_distance_m: float | None = None,
    impulse_aligned: bool = False,
    fit_frequency_range_hz: tuple[float | None, float | None] | None = None,
) -> np.ndarray:
    values = np.asarray(pressure, dtype=np.complex128)
    if impulse_aligned and frequencies_hz is not None:
        freqs = np.asarray(frequencies_hz, dtype=np.float64)
        if freqs.shape == values.shape:
            delay_s = _estimate_impulse_peak_delay_s(
                freqs,
                values,
                polar_distance_m=polar_distance_m,
                fit_frequency_range_hz=fit_frequency_range_hz,
            )
            values = values * np.exp(1j * 2.0 * np.pi * freqs * delay_s)
    return np.degrees(np.angle(values))


def _source_phase_fit_band_hz(
    source_name: str,
    frequencies_hz: np.ndarray,
    *,
    lf_mf_hz: float | None = None,
    mf_hf_hz: float | None = None,
) -> tuple[float | None, float | None] | None:
    source = str(source_name or "").strip().upper()
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    finite = freqs[np.isfinite(freqs) & (freqs > 0.0)]
    if finite.size < 2:
        return None
    lo_hz: float | None = float(np.min(finite))
    hi_hz: float | None = float(np.max(finite))
    if source == "LF" and lf_mf_hz is not None:
        hi_hz = min(float(hi_hz), float(lf_mf_hz))
    elif source == "HF" and mf_hf_hz is not None:
        lo_hz = max(float(lo_hz), float(mf_hf_hz))
    elif source == "MF":
        if lf_mf_hz is not None:
            lo_hz = max(float(lo_hz), float(lf_mf_hz))
        if mf_hf_hz is not None:
            hi_hz = min(float(hi_hz), float(mf_hf_hz))
    return (lo_hz, hi_hz) if lo_hz < hi_hz else None


def _member_phase_fit_band_hz(
    frequencies_hz: np.ndarray,
    member_index: int,
    crossovers_hz: list[float],
) -> tuple[float | None, float | None] | None:
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    finite = freqs[np.isfinite(freqs) & (freqs > 0.0)]
    if finite.size < 2:
        return None
    edges = [
        float(np.min(finite)),
        *[float(xo) for xo in crossovers_hz],
        float(np.max(finite)),
    ]
    if member_index < 0 or member_index + 1 >= len(edges):
        return None
    lo_hz = edges[member_index]
    hi_hz = edges[member_index + 1]
    return (lo_hz, hi_hz) if lo_hz < hi_hz else None


def _source_phase_deg_for_plot(
    pressure: np.ndarray,
    frequencies_hz: np.ndarray,
    args: argparse.Namespace,
    source_name: str,
) -> np.ndarray:
    return _phase_deg_from_pressure(
        pressure,
        frequencies_hz=frequencies_hz,
        polar_distance_m=float(args.polar_distance_m),
        impulse_aligned=True,
        fit_frequency_range_hz=_source_phase_fit_band_hz(
            source_name,
            frequencies_hz,
            lf_mf_hz=args.crossover_lf_mf_hz,
            mf_hf_hz=args.crossover_mf_hf_hz,
        ),
    )


def _group_delay_from_pressure(
    frequencies_hz: np.ndarray,
    pressure: np.ndarray,
    *,
    polar_distance_m: float | None = None,
    warning_label: str = "on-axis response",
) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    values = np.asarray(pressure, dtype=np.complex128)
    omega = 2.0 * np.pi * freqs
    common_delay_s = (
        float(polar_distance_m) / SPEED_OF_SOUND_M_S
        if polar_distance_m is not None
        else 0.0
    )
    residual_pressure = values * np.exp(1j * omega * common_delay_s)
    residual_phase_rad = np.unwrap(np.angle(residual_pressure))
    phase_rad = residual_phase_rad - omega * common_delay_s
    if freqs.size < 2:
        return np.full(freqs.shape, np.nan, dtype=np.float64), phase_rad
    edge_order = 2 if freqs.size > 2 else 1
    group_delay_s = (
        -np.gradient(residual_phase_rad, omega, edge_order=edge_order)
        + common_delay_s
    )
    residual_steps_rad = np.abs(np.diff(residual_phase_rad))
    ambiguous_intervals = residual_steps_rad > (np.pi + 1.0e-12)
    if np.any(ambiguous_intervals):
        ambiguous_bins = np.zeros(freqs.shape, dtype=bool)
        ambiguous_bins[:-1] |= ambiguous_intervals
        ambiguous_bins[1:] |= ambiguous_intervals
        group_delay_s[ambiguous_bins] = np.nan
        band = freqs[ambiguous_bins]
        LOGGER.warning(
            "Group delay residual phase for %s remains ambiguous from %.6g to %.6g Hz; "
            "marked %d bins NaN.",
            warning_label,
            float(np.min(band)),
            float(np.max(band)),
            int(np.count_nonzero(ambiguous_bins)),
        )
    return group_delay_s, phase_rad


def _beamwidth_minus6_db_by_plane(
    pressure_complex: np.ndarray,
    angles_deg: np.ndarray,
    planes: np.ndarray,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]:
    pressure = np.asarray(pressure_complex, dtype=np.complex128)
    angles = np.asarray(angles_deg, dtype=np.float64)
    order = np.argsort(angles)
    sorted_angles = angles[order]
    on_axis_sorted_idx = int(np.argmin(np.abs(sorted_angles)))
    one_sided_positive = sorted_angles[0] >= -1.0e-9 and on_axis_sorted_idx == 0
    threshold_db = -6.0
    widths: dict[str, np.ndarray] = {}
    limited: dict[str, np.ndarray] = {}
    assumed_symmetric: dict[str, np.ndarray] = {}

    def _edge(rel_db: np.ndarray, start: int, step: int) -> tuple[float, bool]:
        idx = start
        next_idx = idx + step
        while 0 <= next_idx < rel_db.size and rel_db[next_idx] >= threshold_db:
            idx = next_idx
            next_idx = idx + step
        if not (0 <= next_idx < rel_db.size):
            return float(sorted_angles[idx]), True
        v0 = float(rel_db[idx])
        v1 = float(rel_db[next_idx])
        a0 = float(sorted_angles[idx])
        a1 = float(sorted_angles[next_idx])
        if abs(v1 - v0) <= 1.0e-12:
            return a1, False
        frac = (threshold_db - v0) / (v1 - v0)
        return a0 + frac * (a1 - a0), False

    for plane_index, plane in enumerate(planes):
        plane_widths = np.zeros(pressure.shape[0], dtype=np.float64)
        plane_limited = np.zeros(pressure.shape[0], dtype=bool)
        for freq_index in range(pressure.shape[0]):
            values = pressure[freq_index, plane_index, order]
            ref = max(float(np.abs(values[on_axis_sorted_idx])), 1.0e-30)
            rel_db = 20.0 * np.log10(np.maximum(np.abs(values), 1.0e-30) / ref)
            left_edge, left_limited = _edge(rel_db, on_axis_sorted_idx, -1)
            right_edge, right_limited = _edge(rel_db, on_axis_sorted_idx, 1)
            if one_sided_positive:
                plane_widths[freq_index] = min(
                    360.0,
                    2.0 * max(0.0, right_edge - float(sorted_angles[on_axis_sorted_idx])),
                )
                plane_limited[freq_index] = right_limited
            else:
                plane_widths[freq_index] = max(0.0, right_edge - left_edge)
                plane_limited[freq_index] = left_limited or right_limited
        widths[str(plane)] = plane_widths
        limited[str(plane)] = plane_limited
        assumed_symmetric[str(plane)] = np.full(
            pressure.shape[0],
            one_sided_positive,
            dtype=bool,
        )
    return widths, limited, assumed_symmetric


def _write_pressure_grid_derived_artifacts(
    out_dir: Path,
    stem: str,
    *,
    label: str,
    frequencies_hz: np.ndarray,
    angles_deg: np.ndarray,
    planes: np.ndarray,
    pressure_complex: np.ndarray,
    polar_distance_m: float,
    mesh_valid_hz: float | None,
    mesh_valid_radiating_hz: float | None,
) -> dict[str, str]:
    safe_stem = _safe_stem(stem)
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    angles = np.asarray(angles_deg, dtype=np.float64)
    plane_names = np.asarray(planes, dtype=str)
    pressure = np.asarray(pressure_complex, dtype=np.complex128)

    directivity_png = out_dir / f"{safe_stem}_directivity_index_power_response.png"
    directivity_csv = out_dir / f"{safe_stem}_directivity_index_power_response.csv"
    directivity_json = out_dir / f"{safe_stem}_directivity_index_power_response.json"
    metrics = _directivity_power_metrics_from_pressure(
        pressure,
        angles,
        polar_distance_m=polar_distance_m,
    )
    save_directivity_power_plot(
        directivity_png,
        freqs,
        directivity_index_db=metrics["directivity_index_db"],
        power_response_db=metrics["power_response_db"],
        title=f"{label} Directivity Index and Power Response",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    _write_csv(
        directivity_csv,
        [
            "frequency_hz",
            "directivity_index_db",
            "power_response_db_spl_avg",
            "acoustic_power_w",
            "on_axis_spl_db",
            "spatial_average_intensity_w_m2",
        ],
        [
            [
                float(freq),
                float(di),
                float(power_db),
                float(power_w),
                float(on_axis),
                float(avg_i),
            ]
            for freq, di, power_db, power_w, on_axis, avg_i in zip(
                freqs,
                metrics["directivity_index_db"],
                metrics["power_response_db"],
                metrics["acoustic_power_w"],
                metrics["on_axis_spl_db"],
                metrics["spatial_average_intensity_w_m2"],
                strict=True,
            )
        ],
    )
    _write_json(
        directivity_json,
        {
            "label": label,
            "frequencies_hz": freqs,
            "directivity_index_db": metrics["directivity_index_db"],
            "power_response_db_spl_avg": metrics["power_response_db"],
            "acoustic_power_w": metrics["acoustic_power_w"],
            "on_axis_spl_db": metrics["on_axis_spl_db"],
            "spatial_average_intensity_w_m2": metrics[
                "spatial_average_intensity_w_m2"
            ],
            "polar_distance_m": float(polar_distance_m),
            "solid_angle_sum_sr": metrics["solid_angle_sum_sr"],
            "solid_angle_coverage_fraction": metrics[
                "solid_angle_coverage_fraction"
            ],
            "polar_angles_deg": metrics["polar_angles_deg"],
            "polar_weights_sr": metrics["polar_weights_sr"],
            "approximation": metrics["approximation"],
        },
    )

    beamwidth_png = out_dir / f"{safe_stem}_beamwidth.png"
    beamwidth_csv = out_dir / f"{safe_stem}_beamwidth.csv"
    beamwidth_json = out_dir / f"{safe_stem}_beamwidth.json"
    (
        beamwidths,
        beamwidth_limited,
        beamwidth_assumed_symmetric,
    ) = _beamwidth_minus6_db_by_plane(
        pressure,
        angles,
        plane_names,
    )
    save_beamwidth_plot(
        beamwidth_png,
        freqs,
        beamwidths,
        title=f"{label} -6 dB Beamwidth",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        assumed_symmetric_from_one_sided_grid=any(
            bool(np.any(flags)) for flags in beamwidth_assumed_symmetric.values()
        ),
    )
    beam_header = ["frequency_hz"]
    for plane in beamwidths:
        beam_header.append(f"{_safe_stem(plane)}_beamwidth_deg")
        beam_header.append(f"{_safe_stem(plane)}_limited_by_grid")
        beam_header.append(
            f"{_safe_stem(plane)}_assumed_symmetric_from_one_sided_grid"
        )
    beam_rows: list[list[float | str | None]] = []
    for freq_index, freq in enumerate(freqs):
        row: list[float | str | None] = [float(freq)]
        for plane in beamwidths:
            row.append(float(beamwidths[plane][freq_index]))
            row.append("true" if bool(beamwidth_limited[plane][freq_index]) else "false")
            row.append(
                "true"
                if bool(beamwidth_assumed_symmetric[plane][freq_index])
                else "false"
            )
        beam_rows.append(row)
    _write_csv(beamwidth_csv, beam_header, beam_rows)
    _write_json(
        beamwidth_json,
        {
            "label": label,
            "frequencies_hz": freqs,
            "beamwidth_deg": beamwidths,
            "limited_by_grid": beamwidth_limited,
            "assumed_symmetric_from_one_sided_grid": beamwidth_assumed_symmetric,
            "threshold_db": -6.0,
        },
    )

    group_delay_png = out_dir / f"{safe_stem}_group_delay.png"
    group_delay_csv = out_dir / f"{safe_stem}_group_delay.csv"
    group_delay_json = out_dir / f"{safe_stem}_group_delay.json"
    on_axis_idx = int(np.argmin(np.abs(angles)))
    on_axis_pressure = pressure[:, 0, on_axis_idx]
    group_delay_s, phase_rad = _group_delay_from_pressure(
        freqs,
        on_axis_pressure,
        polar_distance_m=polar_distance_m,
        warning_label=label,
    )
    save_group_delay_plot(
        group_delay_png,
        freqs,
        group_delay_s,
        title=f"{label} On-Axis Group Delay",
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
    )
    _write_csv(
        group_delay_csv,
        ["frequency_hz", "group_delay_ms", "unwrapped_phase_deg"],
        [
            [float(freq), float(delay_s * 1000.0), float(np.degrees(phase))]
            for freq, delay_s, phase in zip(freqs, group_delay_s, phase_rad, strict=True)
        ],
    )
    _write_json(
        group_delay_json,
        {
            "label": label,
            "frequencies_hz": freqs,
            "group_delay_s": group_delay_s,
            "group_delay_ms": group_delay_s * 1000.0,
            "unwrapped_phase_deg": np.degrees(phase_rad),
            "phase_convention": PRESSURE_NPZ_PHASE_CONVENTION,
            "common_propagation_delay_s": float(polar_distance_m)
            / SPEED_OF_SOUND_M_S,
            "formula": (
                "common_delay_s - d(unwrap(angle(p_engineering * "
                "exp(+j*omega*common_delay_s))))/d(omega)"
            ),
        },
    )
    return {
        "directivity_power_png": str(directivity_png),
        "directivity_power_csv": str(directivity_csv),
        "directivity_power_json": str(directivity_json),
        "beamwidth_png": str(beamwidth_png),
        "beamwidth_csv": str(beamwidth_csv),
        "beamwidth_json": str(beamwidth_json),
        "group_delay_png": str(group_delay_png),
        "group_delay_csv": str(group_delay_csv),
        "group_delay_json": str(group_delay_json),
    }


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


def _mesh_tag_area_vectors_m2(
    mesh_path: Path,
    tag: int,
    *,
    mesh_scale: float,
) -> tuple[np.ndarray, np.ndarray]:
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
    area_vectors = 0.5 * np.cross(p1 - p0, p2 - p0)
    areas = np.linalg.norm(area_vectors, axis=1)
    area = float(np.sum(areas))
    if area <= 0.0 or not np.isfinite(area):
        raise ValueError(f"physical tag {tag} has invalid area {area}")
    return areas, area_vectors


def _mesh_tag_area_m2(mesh_path: Path, tag: int, *, mesh_scale: float) -> float:
    areas, _area_vectors = _mesh_tag_area_vectors_m2(
        mesh_path,
        tag,
        mesh_scale=mesh_scale,
    )
    area = float(np.sum(areas))
    if area <= 0.0 or not np.isfinite(area):
        raise ValueError(f"physical tag {tag} has invalid area {area}")
    return area


def _projected_area_from_area_vectors_m2(
    areas: np.ndarray,
    area_vectors: np.ndarray,
) -> tuple[float, float, np.ndarray, bool]:
    surface_area = float(np.sum(np.asarray(areas, dtype=np.float64)))
    if surface_area <= 0.0 or not np.isfinite(surface_area):
        raise ValueError(f"source surface area must be positive, got {surface_area!r}")
    vector_sum = np.sum(np.asarray(area_vectors, dtype=np.float64), axis=0)
    vector_norm = float(np.linalg.norm(vector_sum))
    if vector_norm <= 1.0e-12 or not np.isfinite(vector_norm):
        raise ValueError("source projected area vector is degenerate")
    axis = vector_sum / vector_norm
    projected_area = abs(float(np.dot(axis, vector_sum)))
    if projected_area <= 0.0 or not np.isfinite(projected_area):
        raise ValueError(f"source projected area is invalid: {projected_area!r}")
    curved = (surface_area - projected_area) > max(1.0e-8 * surface_area, 1.0e-12)
    return projected_area, surface_area, axis, curved


def _mesh_tag_projected_area_m2(
    mesh_path: Path,
    tag: int,
    *,
    mesh_scale: float,
) -> tuple[float, float, np.ndarray, bool]:
    areas, area_vectors = _mesh_tag_area_vectors_m2(
        mesh_path,
        tag,
        mesh_scale=mesh_scale,
    )
    return _projected_area_from_area_vectors_m2(areas, area_vectors)


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


def _voltage_drive_pressure(
    cone_volume_velocity: np.ndarray,
    *,
    frequencies_hz: np.ndarray,
    diaphragm_area_m2: float,
    basis_pressure: np.ndarray,
) -> np.ndarray:
    """Scale a per-unit-acceleration pressure basis to voltage-driven pressure.

    Direct source bases are solved at unit normal ACCELERATION (the
    ``SolveConfig`` default; ``bie.py`` converts ``v_n = weight/(j omega)``),
    so an absolute field for a coupled driver is the basis times the cone
    acceleration ``a = j*omega*U/S`` in the engineering ``e^{+j omega t}``
    convention — NOT the cone velocity ``U/S`` alone.
    """
    omega = 2.0 * np.pi * np.asarray(frequencies_hz, dtype=np.float64)
    acceleration = (
        1j
        * omega
        * np.asarray(cone_volume_velocity, dtype=np.complex128)
        / float(diaphragm_area_m2)
    )
    return acceleration[:, None, None] * np.asarray(
        basis_pressure, dtype=np.complex128
    )


def _source_result_by_name(source_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(result["name"]): result for result in source_results}


def _source_unit_basis_path(source_result: dict[str, Any]) -> Path:
    value = source_result.get("pressure_basis_npz")
    if not value:
        raise RuntimeError(f"{source_result['name']} has no saved pressure basis")
    return Path(str(value))


def _source_active_basis_path(source_result: dict[str, Any]) -> Path:
    value = source_result.get("active_pressure_basis_npz") or source_result.get(
        "pressure_basis_npz"
    )
    if not value:
        raise RuntimeError(f"{source_result['name']} has no saved active pressure basis")
    return Path(str(value))


def _source_unit_basis(source_result: dict[str, Any]) -> PressureBasis:
    basis = source_result.get("_pressure_basis")
    if isinstance(basis, PressureBasis):
        return basis
    return _load_pressure_basis(_source_unit_basis_path(source_result))


def _source_active_basis(source_result: dict[str, Any]) -> PressureBasis:
    basis = source_result.get("_active_pressure_basis")
    if isinstance(basis, PressureBasis):
        return basis
    basis = source_result.get("_pressure_basis")
    if (
        isinstance(basis, PressureBasis)
        and not source_result.get("active_pressure_basis_npz")
    ):
        return basis
    return _load_pressure_basis(_source_active_basis_path(source_result))


def _public_source_result(source_result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in source_result.items()
        if not str(key).startswith("_")
    }


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


def _legacy_passive_cardioid_driver_entry(args: argparse.Namespace) -> str | None:
    legacy_core_attrs = (
        "passive_cardioid_driver_sd_cm2",
        "passive_cardioid_driver_bl_tm",
        "passive_cardioid_driver_re_ohm",
        "passive_cardioid_driver_mmd_g",
        "passive_cardioid_driver_mms_g",
        "passive_cardioid_driver_cms_mm_per_n",
        "passive_cardioid_driver_vas_l",
        "passive_cardioid_driver_fs_hz",
        "passive_cardioid_driver_rms_kg_per_s",
        "passive_cardioid_driver_qms",
    )
    if not any(getattr(args, attr) is not None for attr in legacy_core_attrs):
        return None
    pieces: list[str] = []
    required = (
        ("Sd", args.passive_cardioid_driver_sd_cm2),
        ("Bl", args.passive_cardioid_driver_bl_tm),
        ("Re", args.passive_cardioid_driver_re_ohm),
    )
    for key, value in required:
        if value is not None:
            pieces.append(f"{key}={float(value):g}")
    if args.passive_cardioid_driver_le_mh is not None:
        pieces.append(f"Le={float(args.passive_cardioid_driver_le_mh):g}")
    if args.passive_cardioid_driver_le2_mh is not None:
        pieces.append(f"Le2={float(args.passive_cardioid_driver_le2_mh):g}")
    if args.passive_cardioid_driver_re2_ohm is not None:
        pieces.append(f"Re2={float(args.passive_cardioid_driver_re2_ohm):g}")
    if args.passive_cardioid_driver_mmd_g is not None:
        pieces.append(f"Mmd={float(args.passive_cardioid_driver_mmd_g):g}")
    if args.passive_cardioid_driver_mms_g is not None:
        pieces.append(f"Mms={float(args.passive_cardioid_driver_mms_g):g}")
    if args.passive_cardioid_driver_cms_mm_per_n is not None:
        cms_m_per_n = float(args.passive_cardioid_driver_cms_mm_per_n) * 1.0e-3
        pieces.append(f"Cms={cms_m_per_n:g}")
    if args.passive_cardioid_driver_vas_l is not None:
        pieces.append(f"Vas={float(args.passive_cardioid_driver_vas_l):g}")
    if args.passive_cardioid_driver_fs_hz is not None:
        pieces.append(f"Fs={float(args.passive_cardioid_driver_fs_hz):g}")
    if args.passive_cardioid_driver_rms_kg_per_s is not None:
        pieces.append(f"Rms={float(args.passive_cardioid_driver_rms_kg_per_s):g}")
    if args.passive_cardioid_driver_qms is not None:
        pieces.append(f"Qms={float(args.passive_cardioid_driver_qms):g}")
    if args.passive_cardioid_driver_count is not None:
        pieces.append(f"N={int(args.passive_cardioid_driver_count)}")
    return "MF:" + ",".join(pieces)


def _parse_driver_rear_volume_l(raw_entries: list[str]) -> dict[str, float]:
    volumes: dict[str, float] = {}
    for raw in raw_entries:
        if ":" not in str(raw):
            raise SystemExit(f"--driver-rear-volume-l expects NAME:VALUE, got {raw!r}")
        name, value_text = str(raw).split(":", 1)
        key = name.strip().upper()
        if not key:
            raise SystemExit(f"--driver-rear-volume-l expects NAME:VALUE, got {raw!r}")
        try:
            value = float(value_text.strip())
        except ValueError as exc:
            raise SystemExit(
                f"--driver-rear-volume-l value must be numeric in {raw!r}"
            ) from exc
        if value <= 0.0 or not np.isfinite(value):
            raise SystemExit(f"--driver-rear-volume-l must be positive in {raw!r}")
        if key in volumes:
            raise SystemExit(f"duplicate --driver-rear-volume-l for {name.strip()}")
        volumes[key] = value
    return volumes


def _normalize_driver_lem_args(args: argparse.Namespace) -> None:
    entries = list(args.driver_lem or [])
    legacy_entry = _legacy_passive_cardioid_driver_entry(args)
    explicit_mf = any(str(entry).split(":", 1)[0].strip().upper() == "MF" for entry in entries)
    if legacy_entry is not None and not explicit_mf:
        entries.append(legacy_entry)
        print(
            "DRIVER LEM WARNING: --passive-cardioid-driver-* flags are deprecated; "
            "mapped them to --driver-lem MF:...",
            flush=True,
        )
    elif legacy_entry is not None:
        print(
            "DRIVER LEM WARNING: deprecated --passive-cardioid-driver-* flags "
            "ignored because --driver-lem MF:... was also provided.",
            flush=True,
        )
    try:
        args.driver_lem_specs = parse_driver_lem_cli_entries(entries)
    except DriverLemParseError as exc:
        raise SystemExit(str(exc)) from exc
    args.driver_rear_volume_l_by_name = _parse_driver_rear_volume_l(
        args.driver_rear_volume_l or []
    )
    for spec in args.driver_lem_specs.values():
        for warning in spec.warnings:
            print(f"DRIVER LEM WARNING: {warning}", flush=True)
    args.drive_voltage = (
        args.drive_voltage
        if args.drive_voltage is not None
        else (
            args.passive_cardioid_drive_voltage
            if args.passive_cardioid_drive_voltage is not None
            else 2.83
        )
    )
    args.rg_ohm = (
        args.rg_ohm
        if args.rg_ohm is not None
        else (
            args.passive_cardioid_rg_ohm
            if args.passive_cardioid_rg_ohm is not None
            else 0.0
        )
    )
    if args.drive_voltage <= 0.0 or not np.isfinite(args.drive_voltage):
        raise SystemExit("--drive-voltage must be positive")
    if args.rg_ohm < 0.0 or not np.isfinite(args.rg_ohm):
        raise SystemExit("--rg-ohm must be non-negative")
    if args.passive_cardioid_coupled:
        mf_key = str(args.passive_cardioid_mf_source).strip().upper()
        if mf_key not in args.driver_lem_specs:
            raise SystemExit(
                "--passive-cardioid-coupled requires an MF --driver-lem spec "
                "(deprecated --passive-cardioid-driver-* aliases still work)"
            )


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
    if not hasattr(args, "driver_lem_specs"):
        _normalize_driver_lem_args(args)
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


def _complex_from_jsonable(value: Any) -> Any:
    if isinstance(value, dict) and set(value) >= {"real", "imag"}:
        return complex(float(value["real"]), float(value["imag"]))
    if isinstance(value, list):
        return [_complex_from_jsonable(item) for item in value]
    return value


def _surface_pressure_avg_from_results_json(
    result_json: Path,
    source_tag: int,
) -> np.ndarray | None:
    if not result_json.exists():
        return None
    payload = json.loads(result_json.read_text(encoding="utf-8"))
    surface = payload.get("surface_pressure_avg", {})
    if not isinstance(surface, dict):
        return None
    key = str(source_tag)
    if key not in surface:
        return None
    return np.asarray(_complex_from_jsonable(surface[key]), dtype=np.complex128)


def _solver_surface_avg_to_self_impedance(
    frequencies_hz: np.ndarray,
    surface_pressure_avg_solver: np.ndarray,
    *,
    source_area_m2: float,
) -> np.ndarray:
    """Convert unit-acceleration solver p_avg into engineering z_self."""
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    p_avg = np.asarray(surface_pressure_avg_solver, dtype=np.complex128).reshape(-1)
    if p_avg.shape != freqs.shape:
        raise ValueError(
            "surface_pressure_avg length does not match frequencies "
            f"({p_avg.shape} vs {freqs.shape})"
        )
    area = float(source_area_m2)
    if area <= 0.0 or not np.isfinite(area):
        raise ValueError(f"source_area_m2 must be positive, got {source_area_m2!r}")
    omega = 2.0 * np.pi * freqs
    return np.conjugate(1j * omega * p_avg) / area


def _driver_lem_spec_driver(spec: DriverLemSpec) -> bandpass.Driver:
    params = spec.params
    return bandpass.Driver(
        Sd=float(params["sd_m2"]),
        Bl=float(params["bl_tm"]),
        Re=float(params["re_ohm"]),
        Le=float(params.get("le_h", 0.0)),
        le2_h=None if "le2_h" not in params else float(params["le2_h"]),
        re2_ohm=None if "re2_ohm" not in params else float(params["re2_ohm"]),
        Mmd=None if "mmd_kg" not in params else float(params["mmd_kg"]),
        Mms=None if "mms_kg" not in params else float(params["mms_kg"]),
        Cms=(
            None
            if "cms_m_per_n" not in params
            else float(params["cms_m_per_n"])
        ),
        Vas=None if "vas_m3" not in params else float(params["vas_m3"]),
        Fs=None if "fs_hz" not in params else float(params["fs_hz"]),
        Rms=(
            None
            if "rms_kg_per_s" not in params
            else float(params["rms_kg_per_s"])
        ),
        Qms=None if "qms" not in params else float(params["qms"]),
        n_drivers=int(params.get("n_drivers", 1)),
    )


def _driver_lem_parameter_echo(spec: DriverLemSpec, coupled) -> dict[str, Any]:
    params = spec.params
    echo: dict[str, Any] = {
        "sd_m2": float(params["sd_m2"]),
        "sd_cm2": float(params["sd_m2"]) * 1.0e4,
        "sd_eff_m2": float(coupled.diagnostics["sd_eff_m2"]),
        "bl_tm": float(params["bl_tm"]),
        "re_ohm": float(params["re_ohm"]),
        "le_h": float(params.get("le_h", 0.0)),
        "le_mh": float(params.get("le_h", 0.0)) * 1.0e3,
        "count": int(params.get("n_drivers", 1)),
        "mmd_eff_kg": float(coupled.diagnostics["mmd_eff_kg"]),
        "mmd_eff_g": float(coupled.diagnostics["mmd_eff_kg"]) * 1.0e3,
        "mmd_correction_kg": float(coupled.mmd_correction_kg),
        "mmd_correction_g": float(coupled.mmd_correction_kg) * 1.0e3,
        "mmd_source": str(coupled.diagnostics["mmd_source"]),
    }
    for key, label, scale in (
        ("le2_h", "le2_mh", 1.0e3),
        ("re2_ohm", "re2_ohm", 1.0),
        ("mmd_kg", "mmd_g", 1.0e3),
        ("mms_kg", "mms_g", 1.0e3),
        ("cms_m_per_n", "cms_m_per_n", 1.0),
        ("vas_m3", "vas_l", 1.0e3),
        ("fs_hz", "fs_hz", 1.0),
        ("rms_kg_per_s", "rms_kg_per_s", 1.0),
        ("qms", "qms", 1.0),
        ("xmax_m", "xmax_mm", 1.0e3),
    ):
        if key in params:
            echo[label] = float(params[key]) * scale
    return echo


def _driver_spec_for_source(
    specs: dict[str, DriverLemSpec],
    source_name: str,
) -> DriverLemSpec | None:
    return specs.get(str(source_name).strip().upper())


def _driver_rear_volume_m3(args: argparse.Namespace, source_name: str) -> float | None:
    volumes = getattr(args, "driver_rear_volume_l_by_name", {})
    value = volumes.get(str(source_name).strip().upper())
    if value is None:
        return None
    return float(value) * 1.0e-3


def _source_owned_by_passive_cardioid(args: argparse.Namespace, source_name: str) -> bool:
    return (
        bool(args.passive_cardioid_mf)
        and bool(args.passive_cardioid_coupled)
        and str(args.passive_cardioid_mf_source).strip().upper()
        == str(source_name).strip().upper()
    )


def _source_motion_for_source(args: argparse.Namespace, source_name: str) -> str:
    specs: dict[str, DriverLemSpec] = getattr(args, "driver_lem_specs", {})
    if (
        _driver_spec_for_source(specs, source_name) is not None
        and not _source_owned_by_passive_cardioid(args, source_name)
    ):
        return str(getattr(args, "source_motion", None) or "axial")
    return "normal"


def _driver_coupling_source_area(
    mesh_path: Path,
    args: argparse.Namespace,
    source_tag: int,
    *,
    source_motion: str,
    surface_area_m2: float | None,
    surface_area_provenance: str,
) -> tuple[float, dict[str, Any]]:
    source_motion = str(source_motion or "normal")
    surface_area = None if surface_area_m2 is None else float(surface_area_m2)
    if surface_area is None:
        surface_area = _mesh_tag_area_m2(
            mesh_path,
            int(source_tag),
            mesh_scale=float(args.mesh_scale),
        )
        surface_area_provenance = "mesh_tag_area"
    if source_motion != "axial":
        return surface_area, {
            "source_area_kind": "surface",
            "source_area_provenance": surface_area_provenance,
        }

    projected_area, geometric_surface_area, axis, curved = _mesh_tag_projected_area_m2(
        mesh_path,
        int(source_tag),
        mesh_scale=float(args.mesh_scale),
    )
    if not curved:
        return surface_area, {
            "source_area_kind": "surface",
            "source_area_provenance": surface_area_provenance,
            "source_motion": source_motion,
            "projected_area_m2": float(projected_area),
            "projected_area_axis": axis,
            "projected_area_used": False,
            "projected_area_reason": "axial source tag is planar within tolerance",
        }
    return projected_area, {
        "source_area_kind": "projected",
        "source_area_provenance": "mesh_tag_projected_area",
        "source_motion": source_motion,
        "surface_area_m2": float(geometric_surface_area),
        "surface_area_provenance": "mesh_tag_area",
        "projected_area_m2": float(projected_area),
        "projected_area_axis": axis,
        "projected_area_used": True,
    }


def _pressure_basis_required_for_selected_outputs(args: argparse.Namespace) -> bool:
    return (
        bool(getattr(args, "driver_lem_specs", {}))
        or bool(args.export_vituixcad)
        or not bool(args.skip_derived_acoustics)
        or not bool(args.skip_combined_set)
        or (bool(args.passive_cardioid_mf) and not bool(args.skip_passive_cardioid_set))
    )


def _write_pressure_basis_for_run(args: argparse.Namespace) -> bool:
    return not bool(args.skip_pressure_bases)


def _basis_self_impedance(
    mesh_path: Path,
    args: argparse.Namespace,
    source_result: dict[str, Any],
    basis: PressureBasis,
) -> tuple[np.ndarray, dict[str, Any]]:
    surface = basis.surface_pressure_avg_solver
    provenance = "pressure_basis_npz"
    if surface is None:
        surface = _surface_pressure_avg_from_results_json(
            Path(str(source_result["results_json"])),
            int(source_result["tag"]),
        )
        provenance = "results_json"
    if surface is None:
        raise RuntimeError(
            f"{source_result['name']} has no surface_pressure_avg; cannot derive z_self"
        )
    area = basis.source_area_m2 or source_result.get("source_area_m2")
    area_provenance = "pressure_basis_npz"
    if area is None:
        area_provenance = "mesh_tag_area"
    source_motion = str(
        getattr(basis, "source_motion", None)
        or source_result.get("source_motion")
        or "normal"
    )
    if source_motion != "axial":
        if area is None:
            area = _mesh_tag_area_m2(
                mesh_path,
                int(source_result["tag"]),
                mesh_scale=float(args.mesh_scale),
            )
        z_self = _solver_surface_avg_to_self_impedance(
            basis.frequencies_hz,
            surface,
            source_area_m2=float(area),
        )
        return z_self, {
            "surface_pressure_avg": provenance,
            "surface_pressure_avg_phase_convention": SURFACE_PRESSURE_AVG_PHASE_CONVENTION,
            "source_area_m2": float(area),
            "source_area_provenance": area_provenance,
            "formula": "z_dd_eng = conj(1j*omega*p_avg_solver)/S_tag",
            "mutual_coupling": "driver-driver mutual coupling neglected; self-impedance only",
        }
    area, area_payload = _driver_coupling_source_area(
        mesh_path,
        args,
        int(source_result["tag"]),
        source_motion=source_motion,
        surface_area_m2=None if area is None else float(area),
        surface_area_provenance=area_provenance,
    )
    formula_area = "A_projected" if area_payload["source_area_kind"] == "projected" else "S_tag"
    z_self = _solver_surface_avg_to_self_impedance(
        basis.frequencies_hz,
        surface,
        source_area_m2=float(area),
    )
    payload = {
        "surface_pressure_avg": provenance,
        "surface_pressure_avg_phase_convention": SURFACE_PRESSURE_AVG_PHASE_CONVENTION,
        "source_area_m2": float(area),
        **area_payload,
        "formula": f"z_dd_eng = conj(1j*omega*p_avg_solver)/{formula_area}",
        "mutual_coupling": "driver-driver mutual coupling neglected; self-impedance only",
    }
    return z_self, payload


def _apply_driver_lem_coupling(
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    specs: dict[str, DriverLemSpec] = getattr(args, "driver_lem_specs", {})
    if not specs:
        return None
    by_name = _source_result_by_name(source_results)
    payload: dict[str, Any] = {
        "status": "complete",
        "type": "per_driver_lem_coupling",
        "drive_voltage_v": float(args.drive_voltage),
        "rg_ohm": float(args.rg_ohm),
        "sources": {},
    }
    for source_key, spec in specs.items():
        matching_name = next(
            (name for name in by_name if name.strip().upper() == source_key),
            None,
        )
        if matching_name is None:
            payload["sources"][spec.name] = {
                "status": "skipped",
                "reason": "driver LEM spec has no solved source",
            }
            continue
        source_result = by_name[matching_name]
        if _source_owned_by_passive_cardioid(args, matching_name):
            source_payload = {
                "status": "skipped",
                "reason": "passive-cardioid coupled mode owns this MF source",
            }
            payload["sources"][matching_name] = source_payload
            source_result["driver_lem"] = source_payload
            continue

        try:
            basis = _source_unit_basis(source_result)
            z_self, z_self_payload = _basis_self_impedance(
                mesh_path,
                args,
                source_result,
                basis,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            source_payload = {
                "status": "skipped",
                "reason": str(exc),
            }
            print(
                f"DRIVER LEM WARNING: {matching_name}: {source_payload['reason']}",
                flush=True,
            )
            payload["sources"][matching_name] = source_payload
            source_result["driver_lem"] = source_payload
            continue
        coupled = driver_coupling.coupled_direct_radiator_response(
            basis.frequencies_hz,
            driver=_driver_lem_spec_driver(spec),
            z_self=z_self,
            rear_chamber_volume_m3=_driver_rear_volume_m3(args, matching_name),
            drive_voltage_v=float(args.drive_voltage),
            rg_ohm=float(args.rg_ohm),
        )
        source_area_m2 = float(z_self_payload["source_area_m2"])
        pressure = _voltage_drive_pressure(
            coupled.cone_volume_velocity,
            frequencies_hz=basis.frequencies_hz,
            diaphragm_area_m2=source_area_m2,
            basis_pressure=basis.pressure_complex,
        )
        safe_name = _safe_stem(matching_name)
        active_npz = out_dir / f"{safe_name}_driver_lem_pressure.npz"
        results_npz = out_dir / f"{safe_name}_driver_lem_results.npz"
        response_png = out_dir / f"{safe_name}_frequency_response.png"
        impedance_zma = out_dir / f"{safe_name}_impedance.zma"
        impedance_png = out_dir / f"{safe_name}_impedance.png"
        excursion_png = out_dir / f"{safe_name}_excursion.png"
        active_basis = _active_pressure_basis(
            basis,
            pressure,
            source_normalization="voltage_driven_driver_lem",
            source_area_m2=source_area_m2,
        )
        if not args.skip_driver_lem_artifacts:
            _write_active_pressure_npz(
                active_npz,
                basis,
                pressure,
                source_normalization="voltage_driven_driver_lem",
                source_area_m2=source_area_m2,
            )
        directivity_db = _directivity_from_pressure_array(
            pressure,
            basis.observation_angles_deg,
        )
        on_axis_idx = int(np.argmin(np.abs(basis.observation_angles_deg)))
        on_axis_spl_db = _spl_db_from_pressure(pressure[:, 0, on_axis_idx])
        np.savez_compressed(
            results_npz,
            frequencies_hz=basis.frequencies_hz,
            observation_angles_deg=basis.observation_angles_deg,
            observation_planes=basis.observation_planes,
            pressure_complex=pressure,
            phase_convention=np.asarray(PRESSURE_NPZ_PHASE_CONVENTION),
            directivity_db=directivity_db,
            on_axis_spl_db=on_axis_spl_db,
            cone_volume_velocity=coupled.cone_volume_velocity,
            acoustic_load=coupled.acoustic_load,
            z_self=z_self,
            electrical_input_impedance=coupled.electrical_input_impedance,
            cone_excursion_m=coupled.cone_excursion_m,
            drive_voltage_v=np.asarray(float(args.drive_voltage), dtype=np.float64),
            rg_ohm=np.asarray(float(args.rg_ohm), dtype=np.float64),
            mmd_correction_kg=np.asarray(coupled.mmd_correction_kg, dtype=np.float64),
        ) if not args.skip_driver_lem_artifacts else None
        if not args.skip_per_driver_plots:
            save_frequency_response_plot(
                response_png,
                [
                    FrequencyResponseCurve(
                        frequencies=basis.frequencies_hz,
                        spl_db=on_axis_spl_db,
                        label=f"{matching_name} {args.drive_voltage:g} V",
                        role=_source_role(matching_name),
                    )
                ],
                title=f"{matching_name} Frequency Response",
                ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
                mesh_valid_hz=source_result.get("mesh_valid_freq_max_hz"),
                mesh_valid_radiating_hz=source_result.get("aperture_valid_freq_max_hz"),
                phase_curves=[
                    (
                        basis.frequencies_hz,
                        _source_phase_deg_for_plot(
                            pressure[:, 0, on_axis_idx],
                            basis.frequencies_hz,
                            args,
                            matching_name,
                        ),
                        matching_name,
                        _source_role(matching_name),
                    )
                ],
            )
        if not args.skip_driver_lem_artifacts:
            _write_zma(
                impedance_zma,
                basis.frequencies_hz,
                coupled.electrical_input_impedance,
                comment=(
                    f"{matching_name} driver LEM electrical input impedance "
                    f"at {args.drive_voltage:g} V RMS"
                ),
            )
            save_impedance_plot(
                impedance_png,
                basis.frequencies_hz,
                np.real(coupled.electrical_input_impedance),
                np.imag(coupled.electrical_input_impedance),
                title="Electrical Input Impedance",
                ylabel="|Z| [ohm] / phase-split real+imag [ohm]",
            )
        xmax_m = float(spec.params["xmax_m"]) if "xmax_m" in spec.params else None
        if not args.skip_driver_lem_artifacts:
            save_excursion_plot(
                excursion_png,
                basis.frequencies_hz,
                coupled.cone_excursion_m,
                label=matching_name,
                xmax_m=xmax_m,
            )
        excursion_max_idx = int(np.argmax(coupled.cone_excursion_m))
        z_abs = np.abs(coupled.electrical_input_impedance)
        warnings = list(spec.warnings)
        if str(coupled.diagnostics["mmd_source"]) == "Mms_corrected":
            warning = (
                f"{matching_name}: Mms input path estimates Mmd by subtracting "
                "two-face free-air radiation mass before applying the BEM load"
            )
            warnings.append(warning)
            print(f"DRIVER LEM WARNING: {warning}", flush=True)
        rear_volume = _driver_rear_volume_m3(args, matching_name)
        source_payload = {
            "status": "complete",
            "driver": _driver_lem_parameter_echo(spec, coupled),
            "parse_source": spec.source,
            "parse_warnings": warnings,
            "ignored_keys": list(spec.ignored_keys),
            "drive_voltage_v": float(args.drive_voltage),
            "rg_ohm": float(args.rg_ohm),
            "rear_chamber_volume_l": (
                None if rear_volume is None else rear_volume * 1.0e3
            ),
            "z_self": z_self_payload,
            "excursion_band_max_mm": float(
                coupled.cone_excursion_m[excursion_max_idx] * 1.0e3
            ),
            "excursion_band_max_hz": float(
                basis.frequencies_hz[excursion_max_idx]
            ),
            "z_in_elec_min_ohm": float(np.min(z_abs)),
            "z_in_elec_max_ohm": float(np.max(z_abs)),
            "coupling_assumption": (
                "driver-driver mutual coupling is neglected; each driver uses "
                "its own BEM self-impedance only"
            ),
            "outputs": {
                **(
                    {
                        "active_pressure_npz": str(active_npz),
                        "results_npz": str(results_npz),
                        "impedance_zma": str(impedance_zma),
                        "impedance_png": str(impedance_png),
                        "excursion_png": str(excursion_png),
                    }
                    if not args.skip_driver_lem_artifacts
                    else {}
                ),
                **(
                    {"frequency_response_png": str(response_png)}
                    if not args.skip_per_driver_plots
                    else {}
                ),
            },
        }
        payload["sources"][matching_name] = source_payload
        if source_result.get("pressure_basis_npz"):
            source_result["unit_pressure_basis_npz"] = source_result["pressure_basis_npz"]
        source_result["_active_pressure_basis"] = active_basis
        source_result["_driver_lem_impedance"] = {
            "frequencies_hz": np.asarray(basis.frequencies_hz, dtype=np.float64),
            "impedance_ohm": np.asarray(
                coupled.electrical_input_impedance,
                dtype=np.complex128,
            ),
        }
        if not args.skip_driver_lem_artifacts:
            source_result["active_pressure_basis_npz"] = str(active_npz)
        if not args.skip_per_driver_plots:
            source_result["frequency_response_png"] = str(response_png)
        source_result["on_axis_spl_db"] = on_axis_spl_db
        source_result["driver_lem"] = source_payload
        if not args.skip_driver_lem_artifacts:
            source_result["driver_lem_results_npz"] = str(results_npz)
            source_result["driver_lem_impedance_zma"] = str(impedance_zma)
            source_result["driver_lem_impedance_png"] = str(impedance_png)
            source_result["driver_lem_excursion_png"] = str(excursion_png)
    return payload


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
    if not hasattr(args, "driver_lem_specs"):
        _normalize_driver_lem_args(args)
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

    mf_basis = _source_unit_basis(by_name[mf_name])
    port_basis = _source_unit_basis(by_name[port_name])
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

    if mf_basis.source_area_m2 is not None:
        mf_area_m2 = float(mf_basis.source_area_m2)
    else:
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
        phase_curves=[
            (
                mf_basis.frequencies_hz,
                _phase_deg_from_pressure(
                    mf_basis.pressure_complex[:, 0, on_axis_idx],
                    frequencies_hz=mf_basis.frequencies_hz,
                    polar_distance_m=float(args.polar_distance_m),
                    impulse_aligned=True,
                    fit_frequency_range_hz=(band_lo_hz, band_hi_hz),
                ),
                mf_name,
                "mf",
            ),
            (
                mf_basis.frequencies_hz,
                _phase_deg_from_pressure(
                    port_pressure[:, 0, on_axis_idx],
                    frequencies_hz=mf_basis.frequencies_hz,
                    polar_distance_m=float(args.polar_distance_m),
                    impulse_aligned=True,
                    fit_frequency_range_hz=(band_lo_hz, band_hi_hz),
                ),
                f"{port_name} weighted",
                "other",
            ),
            (
                mf_basis.frequencies_hz,
                _phase_deg_from_pressure(
                    total_pressure[:, 0, on_axis_idx],
                    frequencies_hz=mf_basis.frequencies_hz,
                    polar_distance_m=float(args.polar_distance_m),
                    impulse_aligned=True,
                    fit_frequency_range_hz=(band_lo_hz, band_hi_hz),
                ),
                "MF passive cardioid",
                "combined",
            ),
        ],
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
            mf_driver_spec = _driver_spec_for_source(
                getattr(args, "driver_lem_specs", {}),
                mf_name,
            )
            if mf_driver_spec is None:
                payload["coupled"] = {
                    "status": "skipped",
                    "reason": (
                        "passive-cardioid coupled solve requires an MF "
                        "--driver-lem spec"
                    ),
                }
                _write_json(summary_json, payload)
                return payload
            driver = _driver_lem_spec_driver(mf_driver_spec)
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
                drive_voltage_v=float(args.drive_voltage),
                rg_ohm=float(args.rg_ohm),
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

            coupled_pressure = _voltage_drive_pressure(
                coupled.cone_volume_velocity,
                frequencies_hz=mf_basis.frequencies_hz,
                diaphragm_area_m2=mf_area_m2,
                basis_pressure=total_pressure,
            )
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
            impedance_png = out_dir / "MF_passive_cardioid_impedance.png"
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
                    float(args.drive_voltage),
                    dtype=np.float64,
                ),
                rg_ohm=np.asarray(float(args.rg_ohm), dtype=np.float64),
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
                            f"{args.drive_voltage:g} V"
                        ),
                        role="combined",
                    )
                ],
                title="MF Passive Cardioid Coupled Response",
                ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
                phase_curves=[
                    (
                        mf_basis.frequencies_hz,
                        _phase_deg_from_pressure(
                            coupled_pressure[:, 0, on_axis_idx],
                            frequencies_hz=mf_basis.frequencies_hz,
                            polar_distance_m=float(args.polar_distance_m),
                            impulse_aligned=True,
                            fit_frequency_range_hz=(band_lo_hz, band_hi_hz),
                        ),
                        "MF passive cardioid coupled",
                        "combined",
                    )
                ],
            )
            _write_zma(
                impedance_zma,
                mf_basis.frequencies_hz,
                coupled.electrical_input_impedance,
                comment=(
                    "MF passive-cardioid coupled electrical input impedance "
                    f"at {args.drive_voltage:g} V RMS"
                ),
            )
            save_impedance_plot(
                impedance_png,
                mf_basis.frequencies_hz,
                np.real(coupled.electrical_input_impedance),
                np.imag(coupled.electrical_input_impedance),
                title="Electrical Input Impedance",
                ylabel="|Z| [ohm] / phase-split real+imag [ohm]",
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
                "impedance_png": str(impedance_png),
            }
            payload["outputs"]["coupled_results_npz"] = str(coupled_npz)
            payload["outputs"]["coupled_frequency_response_png"] = (
                str(coupled_response_png)
            )
            payload["outputs"]["impedance_zma"] = str(impedance_zma)
            payload["outputs"]["impedance_png"] = str(impedance_png)
            payload["coupled"] = {
                "status": "complete",
                "driver": _driver_lem_parameter_echo(mf_driver_spec, coupled),
                "drive_voltage_v": float(args.drive_voltage),
                "rg_ohm": float(args.rg_ohm),
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
    source_motion: str = "normal",
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
        source_motion=source_motion,
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
    mesh_path: Path,
    out_dir: Path,
    args: argparse.Namespace,
    *,
    source_name: str,
    source_tag: int,
    freq_max_hz: float | None = None,
    mesh_valid_hz: float | None = None,
    mesh_valid_radiating_hz: float | None = None,
    source_motion: str = "normal",
) -> dict[str, Any]:
    safe_name = _safe_stem(source_name)
    result_json = out_dir / f"{safe_name}_results.json"
    basis_npz = out_dir / f"{safe_name}_pressure_basis.npz"
    heatmap_png = out_dir / f"{safe_name}_directivity_heatmap.png"
    response_png = out_dir / f"{safe_name}_frequency_response.png"
    on_axis_spl_db = _on_axis_spl_db(result)
    on_axis_idx = int(np.argmin(np.abs(result.observation_angles_deg)))
    pressure_engineering = np.conjugate(
        np.asarray(result.pressure_complex, dtype=np.complex128)
    )
    payload = _result_payload(result, source_name=source_name, source_tag=source_tag)
    _write_json(result_json, payload)
    source_area_m2 = None
    if _surface_pressure_avg_for_tag(result, source_tag) is not None:
        source_area_m2 = _mesh_tag_area_m2(
            mesh_path,
            source_tag,
            mesh_scale=float(args.mesh_scale),
        )
    pressure_basis = _pressure_basis_from_result(
        result,
        source_name=source_name,
        source_tag=source_tag,
        source_area_m2=source_area_m2,
        source_motion=source_motion,
    )
    if _write_pressure_basis_for_run(args):
        _write_pressure_basis_npz(
            basis_npz,
            result,
            source_name=source_name,
            source_tag=source_tag,
            source_area_m2=source_area_m2,
            source_motion=source_motion,
        )
    else:
        basis_npz = None
    if not args.skip_per_driver_plots:
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
            phase_curves=[
                (
                    result.frequencies_hz,
                    _source_phase_deg_for_plot(
                        pressure_engineering[:, 0, on_axis_idx],
                        result.frequencies_hz,
                        args,
                        source_name,
                    ),
                    source_name,
                    _source_role(source_name),
                )
            ],
        )
    payload = {
        "name": source_name,
        "tag": source_tag,
        "results_json": str(result_json),
        "pressure_basis_npz": str(basis_npz) if basis_npz is not None else None,
        "directivity_heatmap_png": (
            str(heatmap_png) if not args.skip_per_driver_plots else None
        ),
        "frequency_response_png": (
            str(response_png) if not args.skip_per_driver_plots else None
        ),
        "freq_max_hz": float(args.freq_max_hz if freq_max_hz is None else freq_max_hz),
        "mesh_valid_freq_max_hz": None if mesh_valid_hz is None else float(mesh_valid_hz),
        "aperture_valid_freq_max_hz": (
            None if mesh_valid_radiating_hz is None else float(mesh_valid_radiating_hz)
        ),
        "frequencies_hz": result.frequencies_hz,
        "on_axis_spl_db": on_axis_spl_db,
        "timings": result.timings,
        "source_area_m2": source_area_m2,
        "_pressure_basis": pressure_basis,
    }
    if str(source_motion) != "normal":
        payload["source_motion"] = str(source_motion)
    return payload


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
    surface_pressure_avg = previous_payload.get("surface_pressure_avg", {})
    if basis.surface_pressure_avg_solver is not None:
        surface_pressure_avg = {str(source_tag): basis.surface_pressure_avg_solver}
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
            "surface_pressure_avg": surface_pressure_avg,
            "timings": previous_payload.get("timings", {}),
            "solver_log": previous_payload.get("solver_log", []),
            "mesh_info": previous_payload.get("mesh_info", {}),
            "postprocess_only_regenerated": True,
        },
    )
    if not args.skip_per_driver_plots:
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
            phase_curves=[
                (
                    basis.frequencies_hz,
                    _source_phase_deg_for_plot(
                        basis.pressure_complex[:, 0, on_axis_idx],
                        basis.frequencies_hz,
                        args,
                        source_name,
                    ),
                    source_name,
                    _source_role(source_name),
                )
            ],
        )
    payload = {
        "name": source_name,
        "tag": source_tag,
        "results_json": str(result_json),
        "pressure_basis_npz": str(basis_path),
        "directivity_heatmap_png": (
            str(heatmap_png) if not args.skip_per_driver_plots else None
        ),
        "frequency_response_png": (
            str(response_png) if not args.skip_per_driver_plots else None
        ),
        "freq_max_hz": float(basis.frequencies_hz[-1]),
        "mesh_valid_freq_max_hz": None if mesh_valid_hz is None else float(mesh_valid_hz),
        "aperture_valid_freq_max_hz": (
            None if mesh_valid_radiating_hz is None else float(mesh_valid_radiating_hz)
        ),
        "frequencies_hz": basis.frequencies_hz,
        "on_axis_spl_db": on_axis_spl_db,
        "timings": previous_payload.get("timings", {}),
        "source_area_m2": basis.source_area_m2,
        "postprocess_only": True,
        "_pressure_basis": basis,
    }
    if str(basis.source_motion) != "normal":
        payload["source_motion"] = str(basis.source_motion)
    return payload


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
    source_motion: str = "normal",
) -> dict[str, Any]:
    config = _build_config(
        args,
        source_tag=source_tag,
        frame=frame,
        freq_max_hz=freq_max_hz,
        source_motion=source_motion,
    )
    result = solve(str(mesh_path), config)
    return _write_one_source_outputs(
        result,
        mesh_path,
        out_dir,
        args,
        source_name=source_name,
        source_tag=source_tag,
        freq_max_hz=freq_max_hz,
        mesh_valid_hz=mesh_valid_hz,
        mesh_valid_radiating_hz=mesh_valid_radiating_hz,
        source_motion=source_motion,
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
    source_motion: str = "normal",
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
        args,
        source_tag=group[0][1],
        frame=frame,
        freq_max_hz=freq_max_hz,
        source_motion=source_motion,
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
            mesh_path,
            out_dir,
            args,
            source_name=source_name,
            source_tag=source_tag,
            freq_max_hz=freq_max_hz,
            mesh_valid_hz=mesh_valid.get(source_name),
            mesh_valid_radiating_hz=aperture_valid.get(source_name),
            source_motion=source_motion,
        )
        source_result["multi_source_group"] = [name for name, _ in group]
        source_result["shared_solve_wall_s"] = shared_wall_s
        source_results.append(source_result)
    return source_results


_RADIATION_PAYLOAD_FROM_SOLVER = object()


def _apply_post_solve_derived_outputs(
    mesh_path: Path,
    out_dir: Path,
    layout: SolverOutputLayout,
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
    source_results.sort(key=lambda entry: source_index_by_name.get(entry["name"], 9999))
    driver_lem_payload = _apply_driver_lem_coupling(
        mesh_path,
        layout.driver_lem_dir,
        args,
        source_results=source_results,
    )
    if driver_lem_payload is not None:
        manifest["driver_lem"] = driver_lem_payload
        for source in source_results:
            driver_payload = source.get("driver_lem")
            if not isinstance(driver_payload, dict):
                continue
            outputs = driver_payload.get("outputs", {})
            if not isinstance(outputs, dict):
                continue
            source_name = source["name"]
            if outputs.get("results_npz"):
                manifest["outputs"].setdefault("driver_lem_results_npzs", {})[
                    source_name
                ] = outputs["results_npz"]
            if outputs.get("impedance_zma"):
                manifest["outputs"].setdefault("driver_lem_impedance_zmas", {})[
                    source_name
                ] = outputs["impedance_zma"]
            if outputs.get("impedance_png"):
                manifest["outputs"].setdefault("driver_lem_impedance_pngs", {})[
                    source_name
                ] = outputs["impedance_png"]
            if outputs.get("excursion_png"):
                manifest["outputs"].setdefault("driver_lem_excursion_pngs", {})[
                    source_name
                ] = outputs["excursion_png"]
            if outputs.get("active_pressure_npz"):
                manifest["outputs"].setdefault("driver_lem_active_pressure_npzs", {})[
                    source_name
                ] = outputs["active_pressure_npz"]

    active_bases_by_name: dict[str, PressureBasis] = {}
    basis_required = _pressure_basis_required_for_selected_outputs(args)
    if basis_required:
        for source in source_results:
            source_name = str(source["name"])
            basis = _source_active_basis(source)
            active_bases_by_name[source_name] = basis
            if not args.skip_derived_acoustics:
                derived_outputs = _write_pressure_grid_derived_artifacts(
                    layout.derived_dir,
                    _safe_stem(source_name),
                    label=source_name,
                    frequencies_hz=basis.frequencies_hz,
                    angles_deg=basis.observation_angles_deg,
                    planes=basis.observation_planes,
                    pressure_complex=basis.pressure_complex,
                    polar_distance_m=float(args.polar_distance_m),
                    mesh_valid_hz=source.get("mesh_valid_freq_max_hz"),
                    mesh_valid_radiating_hz=source.get("aperture_valid_freq_max_hz"),
                )
                source.update(
                    {
                        "directivity_power_png": derived_outputs["directivity_power_png"],
                        "directivity_power_csv": derived_outputs["directivity_power_csv"],
                        "directivity_power_json": derived_outputs["directivity_power_json"],
                        "beamwidth_png": derived_outputs["beamwidth_png"],
                        "beamwidth_csv": derived_outputs["beamwidth_csv"],
                        "beamwidth_json": derived_outputs["beamwidth_json"],
                        "group_delay_png": derived_outputs["group_delay_png"],
                        "group_delay_csv": derived_outputs["group_delay_csv"],
                        "group_delay_json": derived_outputs["group_delay_json"],
                    }
                )
    response_curves = [
        FrequencyResponseCurve(
            frequencies=source_result["frequencies_hz"],
            spl_db=source_result["on_axis_spl_db"],
            label=source_result["name"],
            role=_source_role(source_result["name"]),
        )
        for source_result in source_results
    ]
    response_curves.sort(key=lambda curve: source_index_by_name.get(curve.label, 9999))

    combined_mesh_valid_hz = min(source_mesh_valid.values()) if source_mesh_valid else None
    combined_aperture_valid_hz = (
        min(source_aperture_valid.values()) if source_aperture_valid else None
    )
    response_png = layout.combined_dir / "combined_frequency_response.png"
    if not args.skip_combined_set:
        save_frequency_response_plot(
            response_png,
            response_curves,
            title="Fusion WG Metal Direct Source Responses",
            ylabel=f"On-axis SPL at {args.polar_distance_m:g} m [dB]",
            mesh_valid_hz=combined_mesh_valid_hz,
            mesh_valid_radiating_hz=combined_aperture_valid_hz,
            phase_curves=[
                (
                    basis.frequencies_hz,
                    _source_phase_deg_for_plot(
                        basis.pressure_complex[
                            :,
                            0,
                            int(np.argmin(np.abs(basis.observation_angles_deg))),
                        ],
                        basis.frequencies_hz,
                        args,
                        name,
                    ),
                    name,
                    _source_role(name),
                )
                for name, basis in active_bases_by_name.items()
            ],
        )
    manifest["sources"] = [
        _public_source_result(source_result) for source_result in source_results
    ]
    if not args.skip_combined_set:
        manifest["outputs"]["combined_frequency_response_png"] = str(response_png)
    manifest["outputs"]["source_frequency_response_pngs"] = {
        source["name"]: source["frequency_response_png"]
        for source in source_results
        if source.get("frequency_response_png")
    }
    manifest["outputs"]["source_pressure_basis_npzs"] = {
        source["name"]: source["pressure_basis_npz"]
        for source in source_results
        if source.get("pressure_basis_npz")
    }
    manifest["outputs"]["source_results_jsons"] = {
        source["name"]: source["results_json"]
        for source in source_results
        if source.get("results_json")
    }
    manifest["outputs"]["source_directivity_heatmap_pngs"] = {
        source["name"]: source["directivity_heatmap_png"]
        for source in source_results
        if source.get("directivity_heatmap_png")
    }
    if not args.skip_derived_acoustics:
        manifest["outputs"]["source_directivity_power_pngs"] = {
            source["name"]: source["directivity_power_png"] for source in source_results
        }
        manifest["outputs"]["source_directivity_power_csvs"] = {
            source["name"]: source["directivity_power_csv"] for source in source_results
        }
        manifest["outputs"]["source_directivity_power_jsons"] = {
            source["name"]: source["directivity_power_json"] for source in source_results
        }
        manifest["outputs"]["source_beamwidth_pngs"] = {
            source["name"]: source["beamwidth_png"] for source in source_results
        }
        manifest["outputs"]["source_beamwidth_csvs"] = {
            source["name"]: source["beamwidth_csv"] for source in source_results
        }
        manifest["outputs"]["source_beamwidth_jsons"] = {
            source["name"]: source["beamwidth_json"] for source in source_results
        }
        manifest["outputs"]["source_group_delay_pngs"] = {
            source["name"]: source["group_delay_png"] for source in source_results
        }
        manifest["outputs"]["source_group_delay_csvs"] = {
            source["name"]: source["group_delay_csv"] for source in source_results
        }
        manifest["outputs"]["source_group_delay_jsons"] = {
            source["name"]: source["group_delay_json"] for source in source_results
        }
    active_basis_npzs = {
        source["name"]: source["active_pressure_basis_npz"]
        for source in source_results
        if source.get("active_pressure_basis_npz")
    }
    if active_basis_npzs:
        manifest["outputs"]["source_active_pressure_basis_npzs"] = active_basis_npzs

    def _run_crossover_sum(
        mf_override_npz: Path | None,
        mf_override_kind: str,
    ) -> dict[str, Any] | None:
        crossover_payload = _write_crossover_alignment_outputs(
            layout.combined_dir,
            source_results,
            lf_mf_hz=args.crossover_lf_mf_hz,
            mf_hf_hz=args.crossover_mf_hf_hz,
            lf_hf_hz=args.crossover_lf_hf_hz,
            polar_distance_m=float(args.polar_distance_m),
            mesh_valid_hz=combined_mesh_valid_hz,
            mesh_valid_radiating_hz=combined_aperture_valid_hz,
            mf_override_npz=mf_override_npz,
            mf_override_kind=mf_override_kind,
            derived_dir=None if args.skip_derived_acoustics else layout.derived_dir,
        )
        if crossover_payload is not None:
            manifest["crossover_alignment"] = crossover_payload
            outputs = crossover_payload.get("outputs", {})
            if crossover_payload.get("status") == "complete" and isinstance(outputs, dict):
                manifest["outputs"].update(outputs)
            elif crossover_payload.get("status") == "skipped" and mf_override_npz is None:
                # Surface loudly: a skipped combine otherwise leaves no combined
                # directivity heatmap with only a buried manifest note.
                print(
                    "CROSSOVER WARNING: combined/time-aligned outputs skipped — "
                    f"{crossover_payload.get('reason')}",
                    flush=True,
                )
        return crossover_payload

    active_crossover_payload = None
    if not args.skip_combined_set:
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
    if args.skip_radiation_impedance:
        radiation_payload = None
        if port_exit_apertures:
            manifest["radiation_impedance"] = {
                "status": "skipped",
                "reason": "disabled by --skip-radiation-impedance",
            }
    elif radiation_payload_override is _RADIATION_PAYLOAD_FROM_SOLVER:
        radiation_payload = _solve_port_exit_radiation_impedance_matrix(
            mesh_path,
            layout.sources_dir,
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
    passive_payload = None
    if args.skip_passive_cardioid_set:
        if args.passive_cardioid_mf:
            passive_payload = {
                "status": "skipped",
                "reason": "disabled by --skip-passive-cardioid-set",
            }
    else:
        passive_payload = _solve_passive_cardioid_mf(
            mesh_path,
            layout.cardioid_dir,
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
            if outputs.get("impedance_png"):
                manifest["outputs"]["passive_cardioid_impedance_png"] = (
                    outputs["impedance_png"]
                )
    if (
        passive_payload is not None
        and passive_payload.get("status") == "complete"
        and str(args.passive_cardioid_mf_source).strip().upper() == "MF"
    ):
        override = _preferred_passive_cardioid_results(passive_payload)
        if override is not None:
            override_payload = (
                _run_crossover_sum(*override)
                if not args.skip_combined_set
                else None
            )
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


def _run_manifest_dir(out_dir: Path) -> Path:
    return out_dir / RUN_MANIFESTS_DIR_NAME


def _run_manifest_path(out_dir: Path, name: str) -> Path:
    return _run_manifest_dir(out_dir) / name


def _run_manifest_read_path(out_dir: Path, name: str) -> Path:
    preferred = _run_manifest_path(out_dir, name)
    if preferred.exists():
        return preferred
    return out_dir / name


def _run_manifest_write_path(out_dir: Path, name: str) -> Path:
    preferred = _run_manifest_path(out_dir, name)
    legacy = out_dir / name
    if legacy.exists() and not preferred.exists():
        return legacy
    return preferred


def _existing_run_manifest_paths(out_dir: Path, name: str) -> list[Path]:
    paths = []
    for path in (_run_manifest_path(out_dir, name), out_dir / name):
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _read_run_manifest_if_exists(out_dir: Path, name: str) -> dict[str, Any]:
    return _read_json_if_exists(_run_manifest_read_path(out_dir, name))


def _manifest_path_in_run(
    value: Any,
    out_dir: Path,
    *,
    fallback_name: str,
) -> Path:
    candidates: list[Path] = []
    category_dirs = ("sources", "combined", "cardioid", "driver-lem", "derived")
    if value:
        path = Path(str(value))
        candidates.append(path)
        if not path.is_absolute():
            candidates.append(out_dir / path)
        candidates.append(out_dir / path.name)
        for dirname in category_dirs:
            candidates.append(out_dir / dirname / path.name)
    candidates.append(out_dir / fallback_name)
    for dirname in category_dirs:
        candidates.append(out_dir / dirname / fallback_name)
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
    for summary_path in (
        out_dir / "port_exit_radiation_impedance_matrix.summary.json",
        out_dir / "sources" / "port_exit_radiation_impedance_matrix.summary.json",
    ):
        if not summary_path.exists():
            continue
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
    for npz_path in (
        out_dir / "port_exit_radiation_impedance_matrix.npz",
        out_dir / "sources" / "port_exit_radiation_impedance_matrix.npz",
    ):
        if npz_path.exists():
            return {
                "status": "complete",
                "type": "port_exit_radiation_impedance_matrix",
                "outputs": {
                    "npz": str(npz_path),
                    "summary_json": str(npz_path.with_suffix(".summary.json")),
                },
            }
    if required:
        raise RuntimeError(
            "postprocess-only requires existing "
            f"port_exit_radiation_impedance_matrix.npz in {out_dir}"
        )
    return None


def _update_pipeline_summary_manifests(out_dir: Path, direct_manifest: dict[str, Any]) -> None:
    for name in ("fusion_wg_pipeline_manifest.json", "final_summary_manifest.json"):
        for path in _existing_run_manifest_paths(out_dir, name):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["direct_solve"] = direct_manifest
                payload["status"] = "complete"
                payload["finished_at"] = direct_manifest.get(
                    "finished_at",
                    datetime.now().isoformat(timespec="seconds"),
                )
                _write_json(path, payload)


def _missing_postprocess_manifest_error() -> str:
    return (
        "postprocess-only requires direct_solve_manifest.json or "
        "final_summary_manifest.json in the run folder or manifests folder"
    )


def _finalize_run_report(
    out_dir: Path,
    args: argparse.Namespace,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    if args.no_run_report or manifest.get("status") != "complete":
        return
    report_path = out_dir / "report.html"
    manifest.setdefault("outputs", {})["report_html"] = str(report_path)
    _write_json(manifest_path, manifest)
    report_payload = _render_run_report(out_dir)
    if report_payload is not None:
        manifest["report"] = report_payload
        manifest.setdefault("outputs", {})["report_html"] = str(report_path)
        _write_json(manifest_path, manifest)


def _run_postprocess_only(
    mesh_path: Path,
    out_dir: Path,
    layout: SolverOutputLayout,
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
        raise RuntimeError(_missing_postprocess_manifest_error())
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
                layout.sources_dir,
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
    if args.skip_radiation_impedance:
        radiation_required = False
    radiation_payload = None
    if not args.skip_radiation_impedance:
        radiation_payload = _load_existing_radiation_payload(
            out_dir,
            direct_manifest=direct_manifest,
            final_manifest=final_manifest,
            required=radiation_required,
        )
    _apply_post_solve_derived_outputs(
        mesh_path,
        out_dir,
        layout,
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
        "--source-motion",
        choices=("normal", "axial"),
        default=None,
        help=(
            "Override direct driver-radiator source motion. Passive-cardioid "
            "and aperture sources keep their existing motion."
        ),
    )
    parser.add_argument(
        "--plot-theme",
        default="hornlab",
        help="hornlab-plots theme name or custom installed theme. Defaults to hornlab.",
    )
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
    parser.add_argument(
        "--crossover-lf-hf-hz",
        type=float,
        default=None,
        help=(
            "Optional LF/HF LR4 crossover for a two-way with only LF and HF "
            "solved (no MF). Names the LF->HF crossover directly so it is "
            "unambiguous, and overrides any leftover LF/MF or MF/HF value for "
            "the LF+HF pair."
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
    parser.add_argument("--skip-per-driver-plots", action="store_true")
    parser.add_argument("--skip-combined-set", action="store_true")
    parser.add_argument("--skip-passive-cardioid-set", action="store_true")
    parser.add_argument("--skip-driver-lem-artifacts", action="store_true")
    parser.add_argument("--skip-derived-acoustics", action="store_true")
    parser.add_argument("--skip-radiation-impedance", action="store_true")
    parser.add_argument("--skip-pressure-bases", action="store_true")
    parser.add_argument("--no-run-report", action="store_true")
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
    parser.add_argument("--passive-cardioid-driver-le-mh", type=float, default=None)
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
    parser.add_argument("--passive-cardioid-driver-count", type=int, default=None)
    parser.add_argument("--passive-cardioid-drive-voltage", type=float, default=None)
    parser.add_argument("--passive-cardioid-rg-ohm", type=float, default=None)
    parser.add_argument(
        "--driver-lem",
        action="append",
        default=[],
        help=(
            "Per-source driver LEM spec NAME:Key=Value or NAME:/path/to/Hornresp.txt. "
            "Hornresp units: Sd cm2, Mmd/Mms g, Cms m/N, Le mH, Xmax mm. Repeatable."
        ),
    )
    parser.add_argument(
        "--driver-rear-volume-l",
        action="append",
        default=[],
        help="Per-source sealed rear chamber volume NAME:L. Repeatable.",
    )
    parser.add_argument("--drive-voltage", type=float, default=None)
    parser.add_argument("--rg-ohm", type=float, default=None)
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
    set_theme(args.plot_theme)
    mesh_path = args.mesh.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not mesh_path.exists() and not args.postprocess_only:
        raise SystemExit(f"mesh not found: {mesh_path}")
    previous_direct_manifest = _read_run_manifest_if_exists(
        out_dir,
        "direct_solve_manifest.json",
    )
    previous_final_manifest = _read_run_manifest_if_exists(
        out_dir,
        "final_summary_manifest.json",
    )
    layout = SolverOutputLayout(
        out_dir,
        layout_version=_layout_version_for_run(
            postprocess_only=bool(args.postprocess_only),
            previous_direct_manifest=previous_direct_manifest,
            previous_final_manifest=previous_final_manifest,
        ),
    )
    layout.ensure_dirs()
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
        ("--crossover-lf-hf-hz", args.crossover_lf_hf_hz),
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
    if (
        args.export_vituixcad
        and not args.postprocess_only
        and args.skip_combined_set
        and (
            args.crossover_lf_mf_hz is not None
            or args.crossover_mf_hf_hz is not None
            or args.crossover_lf_hf_hz is not None
        )
    ):
        raise SystemExit(
            "--export-vituixcad with crossover frequencies requires the "
            "combined/crossover set; remove --skip-combined-set or omit the "
            "crossover frequencies"
        )
    if args.polar_distance_m <= 0.0:
        raise SystemExit("--polar-distance-m must be positive")
    if args.polar_angle_count <= 0:
        raise SystemExit("--polar-angle-count must be positive")
    if args.complex_k_shift < 0.0:
        raise SystemExit("--complex-k-shift must be non-negative")
    _normalize_driver_lem_args(args)
    _validate_passive_cardioid_args(args)

    frame = _build_frame(args)
    source_motion_by_name = {
        name: _source_motion_for_source(args, name)
        for name, _tag in sources
    }
    started_at = datetime.now().isoformat(timespec="seconds")
    manifest: dict[str, Any] = {
        "pipeline": "solve_fusion_wg_metal",
        "started_at": started_at,
        "mesh": str(mesh_path),
        "output_dir": str(out_dir),
        "layout_version": layout.layout_version,
        "layout": layout.manifest_payload(),
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
            "plot_theme": args.plot_theme,
            "crossover": {
                "lf_mf_hz": args.crossover_lf_mf_hz,
                "mf_hf_hz": args.crossover_mf_hf_hz,
                "lf_hf_hz": args.crossover_lf_hf_hz,
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
            "hornlab_sim_dir": str(HORNLAB_SIM_DIR),
            "hornlab_plots_dir": str(HORNLAB_PLOTS_DIR),
            "hornlab_metal_bem_dir": str(METAL_BEM_DIR),
            "linear_solver": "native-dense-cgesv",
            "source_freq_max_hz": dict(source_freq_max),
            "driver_lem": {
                "drive_voltage_v": args.drive_voltage,
                "rg_ohm": args.rg_ohm,
                "drivers": {
                    spec.name: {
                        "canonical": spec.canonical_payload(),
                        "source": spec.source,
                        "warnings": list(spec.warnings),
                    }
                    for spec in args.driver_lem_specs.values()
                },
                "rear_volume_l": {
                    name: value
                    for name, value in args.driver_rear_volume_l_by_name.items()
                },
            },
            "outputs": {
                "per_driver_plots": not bool(args.skip_per_driver_plots),
                "combined_set": not bool(args.skip_combined_set),
                "passive_cardioid_set": not bool(args.skip_passive_cardioid_set),
                "driver_lem_artifacts": not bool(args.skip_driver_lem_artifacts),
                "derived_acoustics": not bool(args.skip_derived_acoustics),
                "vituixcad_export": bool(args.export_vituixcad),
                "radiation_impedance": not bool(args.skip_radiation_impedance),
                "pressure_bases": _write_pressure_basis_for_run(args),
                "pressure_bases_requested": not bool(args.skip_pressure_bases),
                "run_report": not bool(args.no_run_report),
            },
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
        manifest["config"]["passive_cardioid"]["driver_source"] = (
            "driver_lem_mf_spec"
        )
    manifest_path = _run_manifest_write_path(out_dir, "direct_solve_manifest.json")

    if args.dry_run:
        manifest["sources"] = [
            {
                "name": name,
                "tag": tag,
                **(
                    {"source_motion": source_motion_by_name[name]}
                    if source_motion_by_name[name] != "normal"
                    else {}
                ),
            }
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
                layout,
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
            _finalize_run_report(
                out_dir,
                args,
                manifest=manifest,
                manifest_path=manifest_path,
            )
            if manifest.get("status") == "complete":
                _update_pipeline_summary_manifests(out_dir, manifest)
        print(json.dumps(_jsonable(manifest), indent=2, sort_keys=True))
        return returncode

    source_progress: list[dict[str, Any]] = [
        {
            "name": name,
            "tag": tag,
            "status": "pending",
            **(
                {"source_motion": source_motion_by_name[name]}
                if source_motion_by_name[name] != "normal"
                else {}
            ),
        }
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
        solve_groups: list[tuple[float, str, list[tuple[str, int]]]] = []
        for source_name, source_tag in sources:
            effective_fmax = float(source_freq_max.get(source_name, args.freq_max_hz))
            source_motion = source_motion_by_name[source_name]
            for group_fmax, group_motion, group in solve_groups:
                if group_fmax == effective_fmax and group_motion == source_motion:
                    group.append((source_name, source_tag))
                    break
            else:
                solve_groups.append(
                    (effective_fmax, source_motion, [(source_name, source_tag)])
                )

        for group_fmax, group_motion, group in solve_groups:
            for source_name, source_tag in group:
                source_progress[source_index_by_name[source_name]].update(
                    {
                        "status": "running",
                        "started_at": datetime.now().isoformat(timespec="seconds"),
                        "freq_max_hz": group_fmax,
                        **(
                            {"source_motion": group_motion}
                            if group_motion != "normal"
                            else {}
                        ),
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
                        {"source_motion": group_motion}
                        if group_motion != "normal"
                        else {}
                    ),
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
                        layout.sources_dir,
                        args,
                        source_name=source_name,
                        source_tag=source_tag,
                        frame=frame,
                        freq_max_hz=source_freq_max.get(source_name),
                        mesh_valid_hz=source_mesh_valid.get(source_name),
                        mesh_valid_radiating_hz=source_aperture_valid.get(source_name),
                        source_motion=group_motion,
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
                    layout.sources_dir,
                    args,
                    group=group,
                    frame=frame,
                    freq_max_hz=group_fmax,
                    mesh_valid=source_mesh_valid,
                    aperture_valid=source_aperture_valid,
                    source_motion=group_motion,
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
                    **(
                        {
                            "active_pressure_basis_npz": source_result[
                                "active_pressure_basis_npz"
                            ],
                        }
                        if source_result.get("active_pressure_basis_npz")
                        else {}
                    ),
                    "directivity_heatmap_png": source_result["directivity_heatmap_png"],
                    "frequency_response_png": source_result["frequency_response_png"],
                    "freq_max_hz": source_result["freq_max_hz"],
                    **(
                        {"source_motion": source_result["source_motion"]}
                        if source_result.get("source_motion")
                        else {}
                    ),
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
            layout,
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
        _finalize_run_report(
            out_dir,
            args,
            manifest=manifest,
            manifest_path=manifest_path,
        )

    print(json.dumps(_jsonable(manifest), indent=2, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
