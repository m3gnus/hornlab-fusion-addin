#!/usr/bin/env python3
"""Run the Fusion STEP -> WG Metal BEM mesh preparation pipeline.

This script is intentionally Fusion-agnostic. The Fusion add-in exports the
active design to STEP, then calls this script to:

1. prepare source-tagged WG Metal meshes,
2. run the orientation/4-quarter diagnostic,
3. optionally run direct hornlab-metal-bem solves,
4. write one manifest that Waveguide Generator, direct solve tooling, or a
   human can open.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WG_DIR = REPO_ROOT.parent / "Waveguide Generator"
DEFAULT_TOPOLOGY_TOL = 1e-5
ADDIN_DIR = REPO_ROOT / "fusion-addins" / "WGMetalPipeline"
PREP_SCRIPT = REPO_ROOT / "scripts" / "prepare_step_for_wg_metal.py"
PREP_FEM_SCRIPT = REPO_ROOT / "scripts" / "prepare_fem_chamber.py"
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "diagnose_wg_metal_orientation.py"
SOLVE_SCRIPT = REPO_ROOT / "scripts" / "solve_fusion_wg_metal.py"
REPORT_SCRIPT = REPO_ROOT / "scripts" / "render_run_report.py"
if ADDIN_DIR.is_dir() and str(ADDIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADDIN_DIR))
from fusion_pipeline_launch import (  # noqa: E402
    RUN_MANIFESTS_DIR_NAME,
    build_source_specs,
    load_preset,
    mirror_axes_for_symmetry_planes,
    normalize_source_motion,
    quadrants_for_symmetry_planes,
    symmetry_planes_for_mirror_plane,
)
CANONICAL_SOURCE_TAGS = {
    "LF": 2,
    "MF": 3,
    "HF": 4,
    "PORT_EXIT": 10,
}
CANONICAL_SOLVE_SOURCE_PRIORITY = {
    "HF": 0,
    "MF": 1,
    "LF": 2,
}
# Coupled driver-LEM options forwarded verbatim to the direct solve when
# --passive-cardioid-coupled is set. Kept as a table so tests can assert the
# add-in dialog never emits a coupled flag this pipeline fails to forward.
PASSIVE_CARDIOID_COUPLED_FORWARD_OPTIONS = (
    ("--passive-cardioid-driver-sd-cm2", "passive_cardioid_driver_sd_cm2"),
    ("--passive-cardioid-driver-bl-tm", "passive_cardioid_driver_bl_tm"),
    ("--passive-cardioid-driver-re-ohm", "passive_cardioid_driver_re_ohm"),
    ("--passive-cardioid-driver-le-mh", "passive_cardioid_driver_le_mh"),
    ("--passive-cardioid-driver-le2-mh", "passive_cardioid_driver_le2_mh"),
    ("--passive-cardioid-driver-re2-ohm", "passive_cardioid_driver_re2_ohm"),
    ("--passive-cardioid-driver-mmd-g", "passive_cardioid_driver_mmd_g"),
    ("--passive-cardioid-driver-mms-g", "passive_cardioid_driver_mms_g"),
    (
        "--passive-cardioid-driver-cms-mm-per-n",
        "passive_cardioid_driver_cms_mm_per_n",
    ),
    ("--passive-cardioid-driver-vas-l", "passive_cardioid_driver_vas_l"),
    ("--passive-cardioid-driver-fs-hz", "passive_cardioid_driver_fs_hz"),
    (
        "--passive-cardioid-driver-rms-kg-per-s",
        "passive_cardioid_driver_rms_kg_per_s",
    ),
    ("--passive-cardioid-driver-qms", "passive_cardioid_driver_qms"),
    ("--passive-cardioid-driver-count", "passive_cardioid_driver_count"),
    ("--passive-cardioid-drive-voltage", "passive_cardioid_drive_voltage"),
    ("--passive-cardioid-rg-ohm", "passive_cardioid_rg_ohm"),
)
DRIVER_LEM_REPEATABLE_FORWARD_OPTIONS = (
    ("--driver-lem", "driver_lem"),
    ("--driver-rear-volume-l", "driver_rear_volume_l"),
)
DRIVER_LEM_VALUE_FORWARD_OPTIONS = (
    ("--drive-voltage", "drive_voltage"),
    ("--rg-ohm", "rg_ohm"),
)
OUTPUT_SKIP_FORWARD_OPTIONS = (
    ("--skip-per-driver-plots", "skip_per_driver_plots"),
    ("--skip-combined-set", "skip_combined_set"),
    ("--skip-passive-cardioid-set", "skip_passive_cardioid_set"),
    ("--skip-driver-lem-artifacts", "skip_driver_lem_artifacts"),
    ("--skip-derived-acoustics", "skip_derived_acoustics"),
    ("--skip-radiation-impedance", "skip_radiation_impedance"),
    ("--skip-pressure-bases", "skip_pressure_bases"),
    ("--no-run-report", "no_run_report"),
)
SYMMETRY_PLANE_ALIASES = {
    "": (),
    "none": (),
    "full": (),
    "full-model": (),
    "full model": (),
    "x": ("x0",),
    "x0": ("x0",),
    "left-right": ("x0",),
    "left/right": ("x0",),
    "leftright": ("x0",),
    "yz": ("x0",),
    "y": ("y0",),
    "y0": ("y0",),
    "front-back": ("y0",),
    "front/back": ("y0",),
    "frontback": ("y0",),
    "xz": ("y0",),
    "z": ("z0",),
    "z0": ("z0",),
    "top-bottom": ("z0",),
    "top/bottom": ("z0",),
    "topbottom": ("z0",),
    "xy": ("z0",),
}


def _normalize_bem_formulation(raw: str) -> str:
    value = str(raw).strip().lower().replace("-", "_")
    if value not in {"standard", "complex_k"}:
        raise argparse.ArgumentTypeError(
            "BEM formulation must be one of: standard, complex_k, complex-k"
        )
    return value


def _split_sources(raw_sources: list[str]) -> list[str]:
    sources: list[str] = []
    for raw in raw_sources:
        for part in raw.split(","):
            source = part.strip()
            if source:
                sources.append(source)
    return sources


def _parse_source(source: str) -> tuple[str, str, int | None]:
    parts = [part.strip() for part in source.split(":")]
    if len(parts) not in (2, 3) or not parts[0]:
        raise ValueError(f"invalid source spec {source!r}; expected NAME:RES_MM[:TAG]")
    try:
        resolution = float(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid source resolution in {source!r}") from exc
    if resolution <= 0.0:
        raise ValueError(f"source resolution must be positive in {source!r}")
    if len(parts) == 3:
        return parts[0], parts[1], int(parts[2])
    return parts[0], parts[1], None


def _normalize_sources(raw_sources: list[str]) -> list[str]:
    parsed = [_parse_source(source) for source in raw_sources]
    reserved_explicit_tags = {tag for _, _, tag in parsed if tag is not None}
    used_tags: set[int] = set()
    next_tag = 2
    normalized: list[str] = []
    for name, resolution, tag in parsed:
        if tag is None:
            tag = CANONICAL_SOURCE_TAGS.get(name.strip().upper())
        if tag is None:
            while next_tag in used_tags or next_tag in reserved_explicit_tags:
                next_tag += 1
            tag = next_tag
        if tag in used_tags:
            raise ValueError(f"duplicate source tag {tag} for {name!r}")
        used_tags.add(tag)
        normalized.append(f"{name}:{resolution}:{tag}")
    return normalized


def _source_name_tag(source: str) -> tuple[str, int]:
    name, _, tag = _parse_source(source)
    if tag is None:
        raise ValueError(f"internal error: source {source!r} was not normalized with a tag")
    return name, tag


def _order_sources_for_direct_solve(sources: list[str]) -> list[str]:
    def _sort_key(item: tuple[int, str]) -> tuple[int, int]:
        index, source = item
        name, _ = _source_name_tag(source)
        priority = CANONICAL_SOLVE_SOURCE_PRIORITY.get(
            name.strip().upper(),
            len(CANONICAL_SOLVE_SOURCE_PRIORITY),
        )
        return priority, index

    return [source for _, source in sorted(enumerate(sources), key=_sort_key)]


def _extend_option_value(cmd: list[str], option: str, value: object) -> None:
    """Append an option/value pair, preserving negative-looking values.

    ``argparse`` treats a separate token such as ``-0.139,0,0`` as a new
    option even when the preceding option expects a string. The equals form
    keeps auto-derived frame vectors with negative coordinates parseable.
    """
    text = str(value)
    if text.startswith("-"):
        cmd.append(f"{option}={text}")
    else:
        cmd.extend([option, text])


def _sources_present_in_manifest(sources: list[str], prep_manifest: dict[str, Any]) -> list[str]:
    manifest_sources = prep_manifest.get("sources", {})
    if not isinstance(manifest_sources, dict):
        raise ValueError("prepare manifest has invalid sources payload")
    available_names = set(manifest_sources)
    present = [
        source for source in sources
        if _source_name_tag(source)[0] in available_names
    ]
    if not present:
        raise ValueError("prepare manifest did not contain any requested sources")
    return present


def _symmetry_planes_from_quadrants(quadrants: int) -> tuple[str, ...]:
    if quadrants == 1:
        return ("x0", "y0")
    if quadrants == 14:
        return ("x0",)
    if quadrants == 12:
        return ("y0",)
    if quadrants == 1234:
        return ()
    raise ValueError("--quadrants must be one of 1, 12, 14, 1234")


def _parse_symmetry_planes(raw: str | None, *, quadrants: int) -> tuple[str, ...]:
    if raw is None:
        return _symmetry_planes_from_quadrants(quadrants)
    planes: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if key not in SYMMETRY_PLANE_ALIASES:
            raise ValueError(
                "--symmetry-planes expects comma-separated x0/y0/z0 or "
                "left-right/front-back/top-bottom"
            )
        planes.extend(SYMMETRY_PLANE_ALIASES[key])
    ordered = []
    for plane in ("x0", "y0", "z0"):
        if plane in planes:
            ordered.append(plane)
    if len(ordered) != len(set(planes)):
        raise ValueError("--symmetry-planes contains duplicate planes")
    return tuple(ordered)


def _mirror_axes_for_symmetry_planes(symmetry_planes: tuple[str, ...]) -> str:
    axes = [plane[0] for plane in symmetry_planes]
    return ",".join(axes) if axes else "none"


def _native_symmetry_for_planes(symmetry_planes: tuple[str, ...]) -> str | None:
    if symmetry_planes == ("x0",):
        return "yz"
    if symmetry_planes == ("y0",):
        return "xz"
    if symmetry_planes == ("z0",):
        return "xy"
    if symmetry_planes == ("x0", "y0"):
        return "yz+xz"
    return None


def _run_logged(cmd: list[str], *, cwd: Path, stdout_path: Path, stderr_path: Path) -> int:
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=stdout, stderr=stderr)
    return int(process.returncode)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_manifest_dir(out_dir: Path) -> Path:
    return out_dir / RUN_MANIFESTS_DIR_NAME


def _run_manifest_path(out_dir: Path, name: str) -> Path:
    return _run_manifest_dir(out_dir) / name


def _existing_run_manifest_path(out_dir: Path, name: str) -> Path:
    preferred = _run_manifest_path(out_dir, name)
    if preferred.exists():
        return preferred
    return out_dir / name


def _mesh_frequency_status(prep_manifest: dict[str, Any]) -> str:
    validation = prep_manifest.get("mesh_frequency_validation", {})
    if not isinstance(validation, dict):
        return "unknown"
    status = validation.get("status", "unknown")
    return str(status)


def _per_source_max_valid_hz(
    prep_manifest: dict[str, Any],
    sources: list[str],
) -> dict[str, float]:
    validation = prep_manifest.get("mesh_frequency_validation", {})
    if not isinstance(validation, dict):
        return {}
    per_source = validation.get("per_source", {})
    if not isinstance(per_source, dict):
        return {}
    limits: dict[str, float] = {}
    for source in sources:
        name, _ = _source_name_tag(source)
        source_validation = per_source.get(name)
        if not isinstance(source_validation, dict):
            continue
        raw_limit = source_validation.get(
            "effective_max_valid_frequency_hz",
            source_validation.get("max_valid_frequency_hz"),
        )
        try:
            max_valid = float(raw_limit)
        except (TypeError, ValueError):
            continue
        if max_valid > 0.0:
            limits[name] = max_valid
    return limits


def _per_source_radiating_valid_hz(
    prep_manifest: dict[str, Any],
    sources: list[str],
) -> dict[str, float]:
    """Radiating-surface (patch-only) valid band per source, for plot overlay.

    Distinct from the effective limit: this is undragged by intentionally
    coarse shadow/far surfaces, so the directivity figure shows the trustworthy
    radiating band rather than being pulled down to the cabinet's coarse limit.
    """
    validation = prep_manifest.get("mesh_frequency_validation", {})
    if not isinstance(validation, dict):
        return {}
    radiating = validation.get("per_source_radiating_valid_freq_max_hz", {})
    if not isinstance(radiating, dict):
        return {}
    limits: dict[str, float] = {}
    for source in sources:
        name, _ = _source_name_tag(source)
        raw = radiating.get(name)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            limits[name] = value
    return limits


def _clamped_solve_max_frequency_hz(
    prep_manifest: dict[str, Any],
    sources: list[str],
) -> float | None:
    limits = _per_source_max_valid_hz(prep_manifest, sources)
    if limits:
        return min(limits.values())

    validation = prep_manifest.get("mesh_frequency_validation", {})
    if not isinstance(validation, dict):
        return None
    try:
        max_valid = float(validation["max_valid_frequency_hz"])
    except (KeyError, TypeError, ValueError):
        return None
    return max_valid if max_valid > 0.0 else None


def _open_output_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _open_requested_outputs(args: argparse.Namespace, out_dir: Path) -> None:
    if args.open_output_folder:
        _open_output_folder(out_dir)
    report_path = out_dir / "report.html"
    if args.open_report and not args.no_run_report and report_path.exists():
        _open_output_folder(report_path)


def _render_run_report(out_dir: Path, *, python: str) -> tuple[int, list[str]] | None:
    if not REPORT_SCRIPT.exists():
        return None
    cmd = [str(Path(python).expanduser()), str(REPORT_SCRIPT), str(out_dir)]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), text=True, check=False)
    return int(result.returncode), cmd


def _notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = (
        f"display notification {json.dumps(message)} "
        f"with title {json.dumps(title)}"
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass


def _update_launch_metadata(*, status: str, returncode: int, error: str | None) -> None:
    raw_path = os.environ.get("HORNLAB_FUSION_LAUNCH_METADATA")
    if not raw_path:
        return
    path = Path(raw_path)
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["status"] = status
        metadata["returncode"] = int(returncode)
        metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if error:
            metadata["error"] = error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _finalize_run(args: argparse.Namespace, *, returncode: int, crash_error: str | None) -> None:
    out_dir = args.out.expanduser().resolve()
    manifest: dict[str, Any] = {}
    try:
        manifest = _read_json(
            _existing_run_manifest_path(out_dir, "fusion_wg_pipeline_manifest.json")
        )
    except Exception:
        pass

    status = str(manifest.get("status") or ("failed" if returncode else "complete"))
    if crash_error:
        status = "failed"
    error = crash_error or manifest.get("error")
    _update_launch_metadata(status=status, returncode=returncode, error=error)

    if not args.notify:
        return
    if status == "complete":
        solved = [
            str(source.get("name"))
            for source in manifest.get("direct_solve", {}).get("sources", [])
            if isinstance(source, dict) and source.get("name")
        ]
        detail = f"solved {', '.join(solved)}" if solved else "mesh + diagnostics done"
        adjustment = manifest.get("solve_frequency_adjustment", {})
        if not isinstance(adjustment, dict):
            adjustment = {}
        per_source_clamps = adjustment.get("per_source_freq_max_hz")
        if isinstance(per_source_clamps, dict) and per_source_clamps:
            clamps = ", ".join(
                f"{name} <= {float(freq_max_hz):.0f} Hz"
                for name, freq_max_hz in sorted(per_source_clamps.items())
            )
            detail += f" (mesh-limited: {clamps})"
        elif adjustment.get("clamped_freq_max_hz"):
            detail += (
                f" (mesh-limited to {float(adjustment['clamped_freq_max_hz']):.0f} Hz)"
            )
        else:
            mesh_valid = adjustment.get("mesh_valid_freq_max_hz")
            if isinstance(mesh_valid, dict) and mesh_valid:
                limits = ", ".join(
                    f"{name} {float(freq_max_hz):.0f} Hz"
                    for name, freq_max_hz in sorted(mesh_valid.items())
                )
                detail += f" (full band; mesh-valid to {limits})"
        skipped = adjustment.get("skipped_sources")
        if skipped:
            names = ", ".join(str(item.get("name")) for item in skipped)
            detail += f" (skipped {names})"
        message = f"{out_dir.name}: {detail}"
    else:
        message = f"{out_dir.name}: {error or 'see pipeline manifest'}"
    _notify(f"WG Metal pipeline {status}", message[:240])


def _launch_waveguide_generator(wg_dir: Path, pipeline_manifest: Path) -> None:
    env_prefix = f"WG_FUSION_PIPELINE_MANIFEST={shlex.quote(str(pipeline_manifest))}"
    shell_cmd = f"cd {shlex.quote(str(wg_dir))} && {env_prefix} npm start"
    if sys.platform == "darwin":
        apple_script = (
            'tell application "Terminal"\n'
            "  activate\n"
            f"  do script {json.dumps(shell_cmd)}\n"
            "end tell\n"
        )
        subprocess.Popen(["osascript", "-e", apple_script])
    else:
        subprocess.Popen(["npm", "start"], cwd=str(wg_dir), env={**os.environ, "WG_FUSION_PIPELINE_MANIFEST": str(pipeline_manifest)})


def _argv_has_option(argv: list[str], *options: str) -> bool:
    for token in argv:
        for option in options:
            if token == option or token.startswith(f"{option}="):
                return True
    return False


def _preset_text(settings: dict[str, Any], key: str) -> str | None:
    if key not in settings or settings[key] is None:
        return None
    text = str(settings[key]).strip()
    return text if text else None


def _preset_bool(settings: dict[str, Any], key: str) -> bool | None:
    if key not in settings or settings[key] is None:
        return None
    value = settings[key]
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _set_preset_value(
    args: argparse.Namespace,
    argv: list[str],
    settings: dict[str, Any],
    *,
    key: str,
    dest: str,
    options: tuple[str, ...],
    caster,
) -> None:
    if _argv_has_option(argv, *options):
        return
    raw = _preset_text(settings, key)
    if raw is None:
        return
    try:
        setattr(args, dest, caster(raw))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"preset {key!r} is invalid: {raw!r}") from exc


def _apply_preset_sources(
    args: argparse.Namespace,
    argv: list[str],
    settings: dict[str, Any],
) -> None:
    if _argv_has_option(argv, "--source", "--sources"):
        return
    try:
        sources = build_source_specs(
            lf_mesh_mm=_preset_text(settings, "lf_mesh_mm") or "",
            mf_mesh_mm=_preset_text(settings, "mf_mesh_mm") or "",
            hf_mesh_mm=_preset_text(settings, "hf_mesh_mm") or "",
            port_exit_mesh_mm=_preset_text(settings, "port_exit_mesh_mm") or "",
            port_exit_l_mesh_mm=_preset_text(settings, "port_exit_l_mesh_mm") or "",
            port_exit_r_mesh_mm=_preset_text(settings, "port_exit_r_mesh_mm") or "",
        )
    except ValueError as exc:
        raise SystemExit(f"preset source settings are invalid: {exc}") from exc
    if sources:
        args.source = []
        args.sources = [sources]


def _apply_preset_mirror_plane(
    args: argparse.Namespace,
    argv: list[str],
    settings: dict[str, Any],
) -> None:
    if _argv_has_option(argv, "--symmetry-planes", "--quadrants", "--mirror-axes"):
        return
    mirror_plane = _preset_text(settings, "mirror_plane")
    if not mirror_plane:
        return
    symmetry_planes = symmetry_planes_for_mirror_plane(mirror_plane)
    args.symmetry_planes = symmetry_planes
    quadrants = quadrants_for_symmetry_planes(symmetry_planes)
    if quadrants != "auto":
        args.quadrants = int(quadrants)
    mirror_axes = mirror_axes_for_symmetry_planes(symmetry_planes)
    if mirror_axes != "auto":
        args.mirror_axes = mirror_axes


def _set_preset_bool(
    args: argparse.Namespace,
    argv: list[str],
    settings: dict[str, Any],
    *,
    key: str,
    dest: str,
    options: tuple[str, ...],
    invert: bool = False,
) -> None:
    if _argv_has_option(argv, *options):
        return
    value = _preset_bool(settings, key)
    if value is None:
        return
    setattr(args, dest, (not value) if invert else value)


def _apply_preset_repeatables(
    args: argparse.Namespace,
    argv: list[str],
    settings: dict[str, Any],
) -> None:
    if not _argv_has_option(argv, "--refine"):
        refine = _preset_text(settings, "refine")
        if refine:
            args.refine = [entry.strip() for entry in refine.split(",") if entry.strip()]
    if not _argv_has_option(argv, "--driver-lem"):
        driver_entries = []
        for name, key in (
            ("LF", "lf_driver_lem"),
            ("MF", "mf_driver_lem"),
            ("HF", "hf_driver_lem"),
        ):
            payload = _preset_text(settings, key)
            if payload:
                driver_entries.append(f"{name}:{payload}")
        if driver_entries:
            args.driver_lem = driver_entries
    if not _argv_has_option(argv, "--driver-rear-volume-l"):
        volume_entries = []
        for name, key in (
            ("LF", "lf_driver_rear_volume_l"),
            ("MF", "mf_driver_rear_volume_l"),
            ("HF", "hf_driver_rear_volume_l"),
        ):
            value = _preset_text(settings, key)
            if value:
                volume_entries.append(f"{name}:{value}")
        if volume_entries:
            args.driver_rear_volume_l = volume_entries


def _apply_preset_defaults(args: argparse.Namespace, argv: list[str]) -> None:
    if not args.preset:
        return
    settings = load_preset(args.preset)
    args.preset_path = str(args.preset)
    _apply_preset_sources(args, argv, settings)
    for key, dest, options, caster in (
        ("transition_mm", "transition_mm", ("--transition-mm",), float),
        ("rigid_res_mm", "rigid_res_mm", ("--rigid-res-mm", "--global-res-mm"), float),
        ("freq_min_hz", "freq_min_hz", ("--freq-min-hz",), float),
        ("freq_max_hz", "freq_max_hz", ("--freq-max-hz",), float),
        ("freq_count", "freq_count", ("--freq-count",), int),
        ("freq_spacing", "freq_spacing", ("--freq-spacing",), str),
        ("polar_distance_m", "polar_distance_m", ("--polar-distance-m",), float),
        ("polar_angle_min_deg", "polar_angle_min_deg", ("--polar-angle-min-deg",), float),
        ("polar_angle_max_deg", "polar_angle_max_deg", ("--polar-angle-max-deg",), float),
        ("polar_angle_count", "polar_angle_count", ("--polar-angle-count",), int),
        ("plot_theme", "plot_theme", ("--plot-theme",), str),
        ("crossover_lf_mf_hz", "crossover_lf_mf_hz", ("--crossover-lf-mf-hz",), float),
        ("crossover_mf_hf_hz", "crossover_mf_hf_hz", ("--crossover-mf-hf-hz",), float),
        ("crossover_lf_hf_hz", "crossover_lf_hf_hz", ("--crossover-lf-hf-hz",), float),
        ("source_motion", "source_motion", ("--source-motion",), normalize_source_motion),
        ("drive_voltage", "drive_voltage", ("--drive-voltage",), float),
        ("rg_ohm", "rg_ohm", ("--rg-ohm",), float),
        (
            "passive_cardioid_rear_volume_l",
            "passive_cardioid_rear_volume_l",
            ("--passive-cardioid-rear-volume-l",),
            float,
        ),
        (
            "passive_cardioid_port_length_mm",
            "passive_cardioid_port_length_mm",
            ("--passive-cardioid-port-length-mm",),
            float,
        ),
        (
            "passive_cardioid_port_area_cm2",
            "passive_cardioid_port_area_cm2",
            ("--passive-cardioid-port-area-cm2",),
            float,
        ),
        (
            "passive_cardioid_foam_resistance_pa_s_m3",
            "passive_cardioid_foam_resistance_pa_s_m3",
            ("--passive-cardioid-foam-resistance-pa-s-m3",),
            float,
        ),
    ):
        _set_preset_value(
            args,
            argv,
            settings,
            key=key,
            dest=dest,
            options=options,
            caster=caster,
        )
    if args.freq_spacing not in {"log", "linear"}:
        raise SystemExit("preset 'freq_spacing' must be 'log' or 'linear'")
    _apply_preset_mirror_plane(args, argv, settings)
    if not _argv_has_option(argv, "--mesh-only", "--run-solves"):
        mesh_only = _preset_bool(settings, "mesh_only")
        if mesh_only is not None:
            args.mesh_only = mesh_only
            args.run_solves = not mesh_only
    if not _argv_has_option(argv, "--allow-underresolved-solve", "--underresolved-solve-policy"):
        clamp = _preset_bool(settings, "clamp_to_mesh_limit")
        if clamp is not None:
            args.underresolved_solve_policy = "clamp-per-source" if clamp else "warn"
    _set_preset_bool(
        args,
        argv,
        settings,
        key="show_mesh_valid_markers",
        dest="mesh_valid_markers",
        options=("--mesh-valid-markers", "--no-mesh-valid-markers"),
    )
    for key, dest, options in (
        ("open_wg", "open_wg", ("--open-wg",)),
        ("open_output", "open_output_folder", ("--open-output-folder",)),
        ("open_report", "open_report", ("--open-report",)),
        ("export_vituixcad", "export_vituixcad", ("--export-vituixcad",)),
        ("passive_cardioid_enabled", "passive_cardioid_mf", ("--passive-cardioid-mf",)),
        ("passive_cardioid_coupled", "passive_cardioid_coupled", ("--passive-cardioid-coupled",)),
    ):
        _set_preset_bool(args, argv, settings, key=key, dest=dest, options=options)
    for key, dest, options in (
        ("output_per_driver_plots", "skip_per_driver_plots", ("--skip-per-driver-plots",)),
        ("output_combined_set", "skip_combined_set", ("--skip-combined-set",)),
        ("output_passive_cardioid_set", "skip_passive_cardioid_set", ("--skip-passive-cardioid-set",)),
        ("output_driver_lem_artifacts", "skip_driver_lem_artifacts", ("--skip-driver-lem-artifacts",)),
        ("output_derived_acoustics", "skip_derived_acoustics", ("--skip-derived-acoustics",)),
        ("output_radiation_impedance", "skip_radiation_impedance", ("--skip-radiation-impedance",)),
        ("output_pressure_bases", "skip_pressure_bases", ("--skip-pressure-bases",)),
        ("output_run_report", "no_run_report", ("--no-run-report",)),
    ):
        _set_preset_bool(
            args,
            argv,
            settings,
            key=key,
            dest=dest,
            options=options,
            invert=True,
        )
    _set_preset_bool(
        args,
        argv,
        settings,
        key="passive_cardioid_invert_port",
        dest="passive_cardioid_invert_port",
        options=("--passive-cardioid-invert-port", "--no-passive-cardioid-invert-port"),
    )
    _apply_preset_repeatables(args, argv, settings)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # allow_abbrev=False: preset precedence scans raw argv for exact option
    # tokens; an abbreviated flag would parse but evade the scan, letting the
    # preset silently override an explicitly passed value.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--preset",
        default=None,
        help=(
            "Preset JSON path or saved preset name from "
            "~/Library/Application Support/HornLab/WGMetalPipeline/presets. "
            "Explicit CLI flags override preset values."
        ),
    )
    parser.add_argument("--step", type=Path, required=True, help="STEP file exported from Fusion")
    parser.add_argument("--out", type=Path, required=True, help="Output folder for all generated artifacts")
    parser.add_argument(
        "--fem-chamber-step",
        type=Path,
        default=None,
        help="Separate STEP export containing one watertight chamber air volume.",
    )
    parser.add_argument("--fem-driver-boundary", default="FEM_DRIVER")
    parser.add_argument(
        "--fem-entry",
        action="append",
        default=[],
        help="Shared FEM boundary and exterior BEM source name. Repeatable.",
    )
    parser.add_argument("--fem-output-source", default="MF")
    parser.add_argument("--fem-output-tag", type=int, default=3)
    parser.add_argument("--fem-resolution-mm", type=float, default=8.0)
    parser.add_argument("--fem-loss-factor", type=float, default=0.002)
    parser.add_argument("--fem-area-tolerance", type=float, default=0.05)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Source spec NAME:RES_MM[:TAG]. May be repeated.",
    )
    parser.add_argument(
        "--sources",
        action="append",
        default=[],
        help="Comma-separated source specs, e.g. LF:20:2,HF:5:4.",
    )
    parser.add_argument("--transition-mm", type=float, default=200.0)
    parser.add_argument(
        "--rigid-res-mm",
        "--global-res-mm",
        dest="rigid_res_mm",
        type=float,
        default=None,
        help=(
            "Mesh size for rigid body surfaces away from source refinement. "
            "Defaults to the coarsest declared source resolution."
        ),
    )
    parser.add_argument(
        "--refine",
        action="append",
        default=[],
        help="Per-face refine override NAME:RES_MMmm (forwarded to prepare).",
    )
    parser.add_argument("--quadrants", type=int, default=1, choices=(1, 12, 14, 1234))
    parser.add_argument(
        "--symmetry-planes",
        default=None,
        help=(
            "Comma-separated symmetry cut planes: x0, y0, z0. Aliases: "
            "left-right, front-back, top-bottom, none. 'auto' detects the cut "
            "planes from the prepared mesh free edges. Overrides --quadrants."
        ),
    )
    parser.add_argument("--mirror-axes", default="auto")
    parser.add_argument("--unit-scale-to-m", type=float, default=0.001)
    parser.add_argument("--topology-tol", type=float, default=DEFAULT_TOPOLOGY_TOL)
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for child scripts")
    parser.add_argument("--wg-dir", type=Path, default=DEFAULT_WG_DIR)
    parser.add_argument("--open-wg", action="store_true", help="Launch Waveguide Generator after pipeline completion")
    parser.add_argument("--open-output-folder", action="store_true")
    parser.add_argument("--open-report", action="store_true", help="Open report.html after pipeline completion")
    parser.add_argument("--allow-leaks", action="store_true")
    parser.add_argument(
        "--skip-missing-sources",
        action="store_true",
        help=(
            "Skip requested sources whose STEP shell/style name is absent. "
            "At least one requested source must still be present."
        ),
    )
    parser.add_argument("--mesh-only", action="store_true", help="Stop after mesh preparation and orientation diagnostic")
    parser.add_argument("--run-solves", action="store_true", help="Run direct hornlab-metal-bem solves after diagnostics")
    parser.add_argument(
        "--allow-underresolved-solve",
        action="store_true",
        help=(
            "Run direct solves even when active source patches fail the "
            "conservative mesh frequency check."
        ),
    )
    parser.add_argument(
        "--underresolved-solve-policy",
        choices=("clamp-per-source", "fail", "warn", "clamp"),
        default="warn",
        help=(
            "Behavior when direct solves are requested but active source patches "
            "are too coarse for --freq-max-hz. 'warn' (default) records the "
            "validation warning but solves the requested band; 'clamp-per-source' "
            "solves each source up to its own conservative mesh-valid limit and skips "
            "sources whose limit falls below --freq-min-hz; 'fail' refuses the "
            "solve; 'clamp' lowers the shared solve max frequency to the lowest "
            "valid source limit."
        ),
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Post a macOS notification with the pipeline status when finished.",
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
        help="hornlab-plots theme to forward to the direct solve.",
    )
    parser.add_argument(
        "--crossover-lf-mf-hz",
        type=float,
        default=None,
        help=(
            "Optional LF/MF LR4 crossover frequency for the solver crossover "
            "sum. One frequency is enough for a two-way (two of LF/MF/HF "
            "solved); a three-way needs both."
        ),
    )
    parser.add_argument(
        "--crossover-mf-hf-hz",
        type=float,
        default=None,
        help=(
            "Optional MF/HF LR4 crossover frequency for the solver crossover "
            "sum (see --crossover-lf-mf-hz)."
        ),
    )
    parser.add_argument(
        "--crossover-lf-hf-hz",
        type=float,
        default=None,
        help=(
            "Optional LF/HF LR4 crossover for a two-way with only LF and HF "
            "solved (no MF). Names the LF->HF crossover directly and overrides "
            "any leftover LF/MF or MF/HF value for the LF+HF pair."
        ),
    )
    parser.add_argument("--polar-distance-m", type=float, default=2.0)
    parser.add_argument("--polar-angle-min-deg", type=float, default=0.0)
    parser.add_argument("--polar-angle-max-deg", type=float, default=180.0)
    parser.add_argument("--polar-angle-count", type=int, default=37)
    parser.add_argument(
        "--bem-formulation",
        type=_normalize_bem_formulation,
        default="complex_k",
        metavar="{standard,complex_k,complex-k}",
        help="BEM formulation for direct hornlab-metal-bem solves.",
    )
    parser.add_argument("--complex-k-shift", type=float, default=0.005)
    parser.add_argument(
        "--frame-axis",
        default="auto",
        help=(
            "Observation forward axis. 'auto' (default) snaps the diagnosed "
            "source forward axis to the nearest principal axis and derives "
            "origin/u/v from the inferred mouth centre and cut planes. "
            "Explicit values (+Z, 1,0,0, ...) use --frame-origin/--frame-u/"
            "--frame-v as before."
        ),
    )
    parser.add_argument("--frame-origin", default="0,0,0.31")
    parser.add_argument("--frame-u", default="+X")
    parser.add_argument("--frame-v", default="+Y")
    parser.add_argument(
        "--native-symmetry-plane",
        choices=("auto", "none", "yz", "xz", "xy", "yz+xz"),
        default="auto",
    )
    parser.add_argument(
        "--native-check-open-edges",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Forwarded to the metal solve. Pass --no-native-check-open-edges for "
            "a bare (open-mouth) horn whose mirror-reduced rim is a real free "
            "edge off the symmetry planes."
        ),
    )
    parser.add_argument(
        "--mesh-valid-markers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Overlay the mesh-valid (solid) and aperture-valid (dashed) frequency "
            "markers on the directivity heatmaps and response plots. Pass "
            "--no-mesh-valid-markers to hide them; the solve and its recorded "
            "mesh-valid limits are unaffected."
        ),
    )
    parser.add_argument(
        "--passive-cardioid-mf",
        action="store_true",
        help=(
            "Forward passive-cardioid MF combine settings to the direct solve."
        ),
    )
    parser.add_argument(
        "--export-vituixcad",
        action="store_true",
        help=(
            "Forward the VituixCAD per-angle FRD export to the direct solve."
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
    )
    parser.add_argument("--passive-cardioid-coupled", action="store_true")
    parser.add_argument(
        "--driver-lem",
        action="append",
        default=[],
        help=(
            "Per-source driver LEM spec NAME:Key=Value or NAME:/path/to/Hornresp.txt. "
            "Repeatable."
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
    # Coupled driver-LEM parameters, forwarded verbatim to the direct solve
    # (None = let the solve script's own default apply).
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
    args = parser.parse_args(raw_argv)
    _apply_preset_defaults(args, raw_argv)
    return args


def _quadrants_for_planes(symmetry_planes: tuple[str, ...]) -> int | None:
    return {
        ("x0", "y0"): 1,
        ("x0",): 14,
        ("y0",): 12,
        (): 1234,
    }.get(symmetry_planes)


_FRAME_TRANSVERSE_FOR_AXIS = {
    # axis index -> (u, v); horizontal plane spans axis and u, vertical spans axis and v
    0: ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    1: ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    2: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
}


def _auto_observation_frame(
    orientation_report: dict[str, Any],
    *,
    symmetry_planes: tuple[str, ...],
    unit_scale_to_m: float,
) -> dict[str, Any] | None:
    """Derive the observation frame from cut planes and source orientation.

    The radiation axis of a reduced model must lie in every symmetry cut
    plane, so the cut planes restrict the candidate principal axes (a quarter
    model cut at x=0 and y=0 radiates along z). The triangle-count-weighted
    average of the per-source inferred forward axes picks among the allowed
    candidates and resolves the sign. MEH side-mounted drivers fire into the
    horn rather than along it, which is why their raw normals only vote
    within the plane-constrained candidate set instead of defining the axis.

    The origin is the diagnosed mouth centroid along the chosen axis with
    mirrored coordinates zeroed onto the cut planes.
    """
    entries = [
        entry
        for entry in orientation_report.get("source_frame_inference", [])
        if isinstance(entry, dict) and entry.get("inferred_forward_axis")
    ]
    if not entries:
        return None

    axis_sum = [0.0, 0.0, 0.0]
    mouth_sum = [0.0, 0.0, 0.0]
    weight_sum = 0.0
    for entry in entries:
        weight = float(entry.get("triangles", 1) or 1)
        axis = [float(v) for v in entry["inferred_forward_axis"]]
        mouth = [float(v) for v in entry.get("mouth_center_for_inferred_axis", (0.0, 0.0, 0.0))]
        for i in range(3):
            axis_sum[i] += weight * axis[i]
            mouth_sum[i] += weight * mouth[i]
        weight_sum += weight
    norm = sum(v * v for v in axis_sum) ** 0.5
    if norm <= 0.0 or weight_sum <= 0.0:
        return None
    mean_axis = [v / norm for v in axis_sum]

    mirrored_axes = {plane[0] for plane in symmetry_planes}
    allowed_indices = [i for i, name in enumerate("xyz") if name not in mirrored_axes]
    if not allowed_indices:
        allowed_indices = [0, 1, 2]

    axis_index = max(allowed_indices, key=lambda i: abs(mean_axis[i]))
    sign = 1.0 if mean_axis[axis_index] >= 0.0 else -1.0
    snapped_axis = [0.0, 0.0, 0.0]
    snapped_axis[axis_index] = sign

    allowed_norm = sum(mean_axis[i] ** 2 for i in allowed_indices) ** 0.5
    alignment = (
        abs(mean_axis[axis_index]) / allowed_norm if allowed_norm > 0.0 else 0.0
    )

    warnings = []
    if len(allowed_indices) > 1 and alignment < 0.85:
        warnings.append(
            "inferred forward axis is ambiguous between the candidate axes "
            f"allowed by the cut planes (alignment {alignment:.3f}); check the "
            "frame or pass --frame-axis explicitly"
        )
    if abs(mean_axis[axis_index]) < 0.2:
        warnings.append(
            "inferred forward axis barely projects onto the chosen principal "
            f"axis ({mean_axis[axis_index]:.3f}); the axis sign may be wrong"
        )

    principal_centers = orientation_report.get("principal_axis_mouth_centers", {})
    axis_key = f"{'+' if sign >= 0 else '-'}{'xyz'[axis_index]}"
    mouth_center = principal_centers.get(axis_key)
    if not mouth_center:
        mouth_center = [v / weight_sum for v in mouth_sum]
    mouth_center = [float(v) for v in mouth_center]

    origin = [v * unit_scale_to_m for v in mouth_center]
    for i, name in enumerate("xyz"):
        if name in mirrored_axes:
            origin[i] = 0.0

    u, v = _FRAME_TRANSVERSE_FOR_AXIS[axis_index]
    return {
        "mode": "auto",
        "axis": snapped_axis,
        "axis_candidates": ["xyz"[i] for i in allowed_indices],
        "axis_inferred_mean": mean_axis,
        "axis_alignment": alignment,
        "origin_m": origin,
        "mouth_center_step_units": mouth_center,
        "mouth_center_source": "principal_axis" if principal_centers.get(axis_key) else "source_inference",
        "u": list(u),
        "v": list(v),
        "sources_used": [str(entry.get("name")) for entry in entries],
        "warnings": warnings,
    }


def _run_pipeline(args: argparse.Namespace) -> int:
    raw_sources = _split_sources([*args.source, *args.sources])
    if not raw_sources:
        raise SystemExit("at least one --source or --sources entry is required")
    if args.complex_k_shift < 0.0:
        raise SystemExit("--complex-k-shift must be non-negative")
    for flag, value in (
        ("--crossover-lf-mf-hz", args.crossover_lf_mf_hz),
        ("--crossover-mf-hf-hz", args.crossover_mf_hf_hz),
        ("--crossover-lf-hf-hz", args.crossover_lf_hf_hz),
    ):
        if value is not None and value <= 0.0:
            raise SystemExit(f"{flag} must be positive")
    if (
        args.crossover_lf_mf_hz is not None
        and args.crossover_mf_hf_hz is not None
        and args.crossover_lf_mf_hz >= args.crossover_mf_hf_hz
    ):
        raise SystemExit("--crossover-lf-mf-hz must be below --crossover-mf-hf-hz")
    symmetry_auto = (
        args.symmetry_planes is not None
        and args.symmetry_planes.strip().lower() == "auto"
    )
    try:
        sources = _normalize_sources(raw_sources)
        symmetry_planes: tuple[str, ...] = (
            ()
            if symmetry_auto
            else _parse_symmetry_planes(args.symmetry_planes, quadrants=args.quadrants)
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    fem_entries = [str(name).strip() for name in args.fem_entry if str(name).strip()]
    if args.fem_chamber_step is None and fem_entries:
        raise SystemExit("--fem-entry requires --fem-chamber-step")
    if args.fem_chamber_step is not None:
        if not fem_entries:
            raise SystemExit("--fem-chamber-step requires at least one --fem-entry")
        source_names = {_source_name_tag(source)[0] for source in sources}
        missing = [name for name in fem_entries if name not in source_names]
        if missing:
            raise SystemExit(
                "--fem-entry names not present in --source/--sources: "
                + ", ".join(missing)
            )
        if args.fem_output_source in source_names:
            raise SystemExit(
                "--fem-output-source must not also be a direct BEM source"
            )
        if args.fem_resolution_mm <= 0.0:
            raise SystemExit("--fem-resolution-mm must be positive")
        if args.fem_loss_factor < 0.0:
            raise SystemExit("--fem-loss-factor must be non-negative")
        if not (0.0 <= args.fem_area_tolerance < 1.0):
            raise SystemExit("--fem-area-tolerance must be in [0, 1)")

    step_path = args.step.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    wg_dir = args.wg_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    mesh_dir = out_dir / "mesh"
    manifests_dir = _run_manifest_dir(out_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    mesh_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    prep_cmd = [
        str(Path(args.python).expanduser()),
        str(PREP_SCRIPT),
        "--step",
        str(step_path),
        "--out",
        str(mesh_dir),
        "--transition-mm",
        str(args.transition_mm),
        "--quadrants",
        str(args.quadrants),
        "--symmetry-planes",
        "auto" if symmetry_auto else (",".join(symmetry_planes) if symmetry_planes else "none"),
        "--unit-scale-to-m",
        str(args.unit_scale_to_m),
        "--topology-tol",
        str(args.topology_tol),
        "--requested-max-frequency-hz",
        str(args.freq_max_hz),
    ]
    if args.rigid_res_mm is not None:
        prep_cmd.extend(["--rigid-res-mm", str(args.rigid_res_mm)])
    for refine in args.refine:
        prep_cmd.extend(["--refine", refine])
    if args.allow_leaks:
        prep_cmd.append("--allow-leaks")
    if args.skip_missing_sources:
        prep_cmd.append("--skip-missing-sources")
    if args.fem_chamber_step is not None:
        prep_cmd.append("--exclude-solid-breps")
    for source in sources:
        prep_cmd.extend(["--source", source])

    started_at = datetime.now().isoformat(timespec="seconds")
    prep_returncode = _run_logged(
        prep_cmd,
        cwd=REPO_ROOT,
        stdout_path=logs_dir / "prepare_step_for_wg_metal.stdout.log",
        stderr_path=logs_dir / "prepare_step_for_wg_metal.stderr.log",
    )
    prep_manifest_path = mesh_dir / "manifest.json"
    pipeline_manifest_path = _run_manifest_path(out_dir, "fusion_wg_pipeline_manifest.json")
    final_summary_manifest_path = _run_manifest_path(out_dir, "final_summary_manifest.json")

    pipeline_manifest: dict[str, Any] = {
        "pipeline": "fusion_step_to_wg_metal",
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "status": "failed" if prep_returncode else "prepared",
        "step": str(step_path),
        "output_dir": str(out_dir),
        "mesh_dir": str(mesh_dir),
        "manifests_dir": str(manifests_dir),
        "waveguide_generator_dir": str(wg_dir),
        "commands": {
            "prepare": prep_cmd,
        },
        "logs": {
            "prepare_stdout": str(logs_dir / "prepare_step_for_wg_metal.stdout.log"),
            "prepare_stderr": str(logs_dir / "prepare_step_for_wg_metal.stderr.log"),
        },
        "requested_sources": sources,
        "sources": sources,
        "quadrants": args.quadrants,
        "symmetry_planes": "auto" if symmetry_auto else list(symmetry_planes),
        "symmetry_planes_mode": "auto" if symmetry_auto else "explicit",
        "rigid_res_mm": args.rigid_res_mm,
    }

    if prep_returncode != 0:
        pipeline_manifest["error"] = "prepare_step_for_wg_metal.py failed"
        pipeline_manifest["returncode"] = prep_returncode
        _write_json(pipeline_manifest_path, pipeline_manifest)
        _open_requested_outputs(args, out_dir)
        return prep_returncode

    fem_mesh_path: Path | None = None
    if args.fem_chamber_step is not None:
        fem_step_path = args.fem_chamber_step.expanduser().resolve()
        fem_dir = out_dir / "fem"
        fem_dir.mkdir(parents=True, exist_ok=True)
        fem_cmd = [
            str(Path(args.python).expanduser()),
            str(PREP_FEM_SCRIPT),
            "--step",
            str(fem_step_path),
            "--out",
            str(fem_dir),
            "--resolution-mm",
            str(args.fem_resolution_mm),
            "--unit-scale-to-m",
            str(args.unit_scale_to_m),
            "--boundary",
            str(args.fem_driver_boundary),
        ]
        for name in fem_entries:
            fem_cmd.extend(["--boundary", name])
        fem_returncode = _run_logged(
            fem_cmd,
            cwd=REPO_ROOT,
            stdout_path=logs_dir / "prepare_fem_chamber.stdout.log",
            stderr_path=logs_dir / "prepare_fem_chamber.stderr.log",
        )
        pipeline_manifest["commands"]["prepare_fem_chamber"] = fem_cmd
        pipeline_manifest["logs"].update(
            {
                "prepare_fem_chamber_stdout": str(
                    logs_dir / "prepare_fem_chamber.stdout.log"
                ),
                "prepare_fem_chamber_stderr": str(
                    logs_dir / "prepare_fem_chamber.stderr.log"
                ),
            }
        )
        if fem_returncode != 0:
            pipeline_manifest["status"] = "failed"
            pipeline_manifest["error"] = "prepare_fem_chamber.py failed"
            pipeline_manifest["returncode"] = fem_returncode
            _write_json(pipeline_manifest_path, pipeline_manifest)
            _open_requested_outputs(args, out_dir)
            return fem_returncode
        fem_mesh_path = fem_dir / "fem_chamber.msh"
        fem_manifest_path = fem_dir / "fem_chamber_mesh_manifest.json"
        pipeline_manifest["fem_chamber"] = {
            "status": "prepared",
            "step": str(fem_step_path),
            "mesh": str(fem_mesh_path),
            "manifest": str(fem_manifest_path),
            "driver_boundary": str(args.fem_driver_boundary),
            "entry_boundaries": list(fem_entries),
            "output_source": str(args.fem_output_source),
            "output_tag": int(args.fem_output_tag),
            "loss_factor": float(args.fem_loss_factor),
        }

    prep_manifest = _read_json(prep_manifest_path)
    try:
        sources = _sources_present_in_manifest(sources, prep_manifest)
    except ValueError as exc:
        pipeline_manifest["status"] = "failed"
        pipeline_manifest["error"] = str(exc)
        _write_json(pipeline_manifest_path, pipeline_manifest)
        _open_requested_outputs(args, out_dir)
        return 1
    pipeline_manifest["sources"] = sources
    pipeline_manifest["skipped_sources"] = prep_manifest.get("skipped_sources", {})
    pipeline_manifest["density"] = prep_manifest.get("density", {})
    pipeline_manifest["mesh_repair"] = prep_manifest.get("mesh_repair", {})
    pipeline_manifest["topology"] = prep_manifest.get("topology", {})
    pipeline_manifest["geometry_healed"] = bool(prep_manifest.get("geometry_healed", False))
    pipeline_manifest["geometry_healing_mode"] = prep_manifest.get(
        "geometry_healing_mode",
        "none",
    )
    pipeline_manifest["mesh_frequency_validation"] = prep_manifest.get(
        "mesh_frequency_validation",
        {},
    )

    if symmetry_auto:
        symmetry_planes = tuple(prep_manifest.get("symmetry_planes") or ())
        pipeline_manifest["symmetry_planes"] = list(symmetry_planes)
        pipeline_manifest["quadrants"] = _quadrants_for_planes(symmetry_planes)
    mirror_axes = _mirror_axes_for_symmetry_planes(symmetry_planes)
    if args.mirror_axes and args.mirror_axes != "auto":
        mirror_axes = args.mirror_axes
    native_symmetry_plane = (
        _native_symmetry_for_planes(symmetry_planes)
        if args.native_symmetry_plane == "auto"
        else (None if args.native_symmetry_plane == "none" else args.native_symmetry_plane)
    )

    run_solves = bool(args.run_solves and not args.mesh_only)
    diagnose_sources = []
    for source in sources:
        name, tag = _source_name_tag(source)
        diagnose_sources.extend(["--source", f"{name}:{tag}"])

    tagged_mesh_path = mesh_dir / "tagged_sources.msh"
    diagnose_out = mesh_dir
    diagnose_cmd = [
        str(Path(args.python).expanduser()),
        str(DIAGNOSE_SCRIPT),
        "--mesh",
        str(tagged_mesh_path),
        "--out",
        str(diagnose_out),
        "--mirror-axes",
        mirror_axes,
        "--tol",
        str(args.topology_tol),
        "--unit-scale-to-m",
        str(args.unit_scale_to_m),
        *diagnose_sources,
    ]
    diagnose_returncode = _run_logged(
        diagnose_cmd,
        cwd=REPO_ROOT,
        stdout_path=logs_dir / "diagnose_wg_metal_orientation.stdout.log",
        stderr_path=logs_dir / "diagnose_wg_metal_orientation.stderr.log",
    )
    pipeline_manifest["commands"]["diagnose"] = diagnose_cmd
    pipeline_manifest["logs"].update(
        {
            "diagnose_stdout": str(logs_dir / "diagnose_wg_metal_orientation.stdout.log"),
            "diagnose_stderr": str(logs_dir / "diagnose_wg_metal_orientation.stderr.log"),
        }
    )
    pipeline_manifest["prep_manifest"] = str(prep_manifest_path)
    pipeline_manifest["tagged_mesh_step_units"] = prep_manifest.get("tagged_mesh_step_units")
    pipeline_manifest["wg_source_meshes_m"] = prep_manifest.get("wg_source_meshes_m", {})
    pipeline_manifest["solver_ready"] = bool(prep_manifest.get("solver_ready"))

    if diagnose_returncode != 0:
        pipeline_manifest["status"] = "failed"
        pipeline_manifest["error"] = "diagnose_wg_metal_orientation.py failed"
        pipeline_manifest["returncode"] = diagnose_returncode
        _write_json(pipeline_manifest_path, pipeline_manifest)
        _open_requested_outputs(args, out_dir)
        return diagnose_returncode

    orientation_report_path = diagnose_out / "orientation_report.json"
    orientation_report = _read_json(orientation_report_path)
    expanded_mesh = orientation_report.get("expanded_mesh", {})
    solve_mesh_path = tagged_mesh_path
    solve_native_symmetry_plane = native_symmetry_plane
    if native_symmetry_plane is None and symmetry_planes:
        expanded_mesh_path = expanded_mesh.get("mesh")
        if not expanded_mesh_path:
            pipeline_manifest["status"] = "failed"
            pipeline_manifest["error"] = "diagnostic did not produce expanded mesh for non-native symmetry"
            _write_json(pipeline_manifest_path, pipeline_manifest)
            _open_requested_outputs(args, out_dir)
            return 1
        solve_mesh_path = Path(str(expanded_mesh_path))
        solve_native_symmetry_plane = None
    pipeline_manifest["orientation_report"] = str(orientation_report_path)
    pipeline_manifest["expanded_mesh"] = expanded_mesh
    pipeline_manifest["expanded_4quarter"] = orientation_report.get("expanded_4quarter", {})
    wg_source_meshes_m = (
        orientation_report.get("wg_source_meshes_m")
        or prep_manifest.get("wg_source_meshes_m", {})
    )
    pipeline_manifest["wg_source_meshes_m"] = wg_source_meshes_m
    pipeline_manifest["solve_mesh"] = str(solve_mesh_path)
    pipeline_manifest["native_symmetry_plane"] = solve_native_symmetry_plane

    frame_axis = args.frame_axis
    frame_origin = args.frame_origin
    frame_u = args.frame_u
    frame_v = args.frame_v
    if str(args.frame_axis).strip().lower() == "auto":
        auto_frame = _auto_observation_frame(
            orientation_report,
            symmetry_planes=symmetry_planes,
            unit_scale_to_m=args.unit_scale_to_m,
        )
        if auto_frame is None:
            pipeline_manifest["status"] = "failed"
            pipeline_manifest["error"] = (
                "could not infer an observation frame from the orientation "
                "diagnostic; pass --frame-axis/--frame-origin explicitly"
            )
            _write_json(pipeline_manifest_path, pipeline_manifest)
            _open_requested_outputs(args, out_dir)
            return 2
        frame_axis = ",".join(f"{c:g}" for c in auto_frame["axis"])
        frame_origin = ",".join(f"{c:.6g}" for c in auto_frame["origin_m"])
        frame_u = ",".join(f"{c:g}" for c in auto_frame["u"])
        frame_v = ",".join(f"{c:g}" for c in auto_frame["v"])
        pipeline_manifest["observation_frame"] = auto_frame
    else:
        pipeline_manifest["observation_frame"] = {
            "mode": "explicit",
            "axis": frame_axis,
            "origin": frame_origin,
            "u": frame_u,
            "v": frame_v,
        }
    pipeline_manifest["waveguide_generator"] = {
        "launch_command": (
            f"cd {shlex.quote(str(wg_dir))} && "
            f"WG_FUSION_PIPELINE_MANIFEST={shlex.quote(str(pipeline_manifest_path))} npm start"
        ),
        "import_mesh": str(expanded_mesh.get("mesh") or solve_mesh_path),
        "per_source_meshes_m": wg_source_meshes_m,
    }
    solve_manifest_path = _run_manifest_path(out_dir, "direct_solve_manifest.json")
    solve_returncode = 0
    solve_freq_max_hz = args.freq_max_hz
    solve_sources = _order_sources_for_direct_solve(sources)
    solve_source_freq_max: dict[str, float] = {}
    if run_solves:
        if (
            not args.allow_underresolved_solve
            and _mesh_frequency_status(prep_manifest) == "invalid"
        ):
            pipeline_manifest["underresolved_solve_policy"] = args.underresolved_solve_policy
            if args.underresolved_solve_policy == "fail":
                pipeline_manifest["status"] = "failed"
                pipeline_manifest["error"] = (
                    "active source mesh frequency validation failed; rerun with finer source mesh, "
                    "lower --freq-max-hz, use --underresolved-solve-policy clamp-per-source/warn/clamp, "
                    "or pass --allow-underresolved-solve for debugging"
                )
                _write_json(pipeline_manifest_path, pipeline_manifest)
                _open_requested_outputs(args, out_dir)
                return 2

            if args.underresolved_solve_policy == "warn":
                limits = _per_source_max_valid_hz(prep_manifest, sources)
                pipeline_manifest["solve_frequency_adjustment"] = {
                    "policy": "warn",
                    "requested_freq_min_hz": float(args.freq_min_hz),
                    "requested_freq_max_hz": float(args.freq_max_hz),
                    "mesh_valid_freq_max_hz": {
                        name: float(limit)
                        for name, limit in limits.items()
                        if limit < args.freq_max_hz
                    },
                    "reason": "active source mesh frequency validation failed but policy is warning-only",
                }
            elif args.underresolved_solve_policy == "clamp-per-source":
                limits = _per_source_max_valid_hz(prep_manifest, sources)
                kept_sources: list[str] = []
                skipped_solve_sources: list[dict[str, Any]] = []
                for source in sources:
                    name, _ = _source_name_tag(source)
                    limit = limits.get(name)
                    if limit is not None and limit < args.freq_min_hz:
                        skipped_solve_sources.append({
                            "name": name,
                            "max_valid_frequency_hz": float(limit),
                            "reason": (
                                "mesh-valid solve frequency limit is below --freq-min-hz "
                                f"({limit:.6g} Hz < {args.freq_min_hz:.6g} Hz)"
                            ),
                        })
                        continue
                    kept_sources.append(source)
                    if limit is not None and limit < args.freq_max_hz:
                        solve_source_freq_max[name] = float(limit)
                if not kept_sources:
                    pipeline_manifest["status"] = "failed"
                    pipeline_manifest["error"] = (
                        "all source patches are too coarse to solve above --freq-min-hz; "
                        "rerun with finer source meshes"
                    )
                    pipeline_manifest["solve_frequency_adjustment"] = {
                        "policy": "clamp-per-source",
                        "requested_freq_min_hz": float(args.freq_min_hz),
                        "requested_freq_max_hz": float(args.freq_max_hz),
                        "skipped_sources": skipped_solve_sources,
                    }
                    _write_json(pipeline_manifest_path, pipeline_manifest)
                    _open_requested_outputs(args, out_dir)
                    return 2
                solve_sources = kept_sources
                pipeline_manifest["solve_frequency_adjustment"] = {
                    "policy": "clamp-per-source",
                    "requested_freq_min_hz": float(args.freq_min_hz),
                    "requested_freq_max_hz": float(args.freq_max_hz),
                    "per_source_freq_max_hz": dict(solve_source_freq_max),
                    "skipped_sources": skipped_solve_sources,
                    "reason": (
                        "active source mesh frequency validation failed; each source "
                        "solves up to its own conservative mesh-valid limit"
                    ),
                }
            else:
                clamped_freq_max_hz = _clamped_solve_max_frequency_hz(prep_manifest, sources)
                if clamped_freq_max_hz is None:
                    pipeline_manifest["status"] = "failed"
                    pipeline_manifest["error"] = "could not determine a mesh-valid solve frequency limit"
                    _write_json(pipeline_manifest_path, pipeline_manifest)
                    _open_requested_outputs(args, out_dir)
                    return 2
                if clamped_freq_max_hz < args.freq_min_hz:
                    pipeline_manifest["status"] = "failed"
                    pipeline_manifest["error"] = (
                        "mesh-valid solve frequency limit is below --freq-min-hz "
                        f"({clamped_freq_max_hz:.6g} Hz < {args.freq_min_hz:.6g} Hz)"
                    )
                    pipeline_manifest["solve_frequency_adjustment"] = {
                        "policy": "clamp",
                        "requested_freq_min_hz": float(args.freq_min_hz),
                        "requested_freq_max_hz": float(args.freq_max_hz),
                        "clamped_freq_max_hz": float(clamped_freq_max_hz),
                    }
                    _write_json(pipeline_manifest_path, pipeline_manifest)
                    _open_requested_outputs(args, out_dir)
                    return 2
                solve_freq_max_hz = min(args.freq_max_hz, clamped_freq_max_hz)
                pipeline_manifest["solve_frequency_adjustment"] = {
                    "policy": "clamp",
                    "requested_freq_min_hz": float(args.freq_min_hz),
                    "requested_freq_max_hz": float(args.freq_max_hz),
                    "clamped_freq_max_hz": float(solve_freq_max_hz),
                    "reason": "active source mesh frequency validation failed",
                }
        elif args.allow_underresolved_solve:
            pipeline_manifest["underresolved_solve_policy"] = "allow"

        solve_cmd = [
            str(Path(args.python).expanduser()),
            str(SOLVE_SCRIPT),
            "--mesh",
            str(solve_mesh_path),
            "--out",
            str(out_dir),
            "--freq-min-hz",
            str(args.freq_min_hz),
            "--freq-max-hz",
            str(solve_freq_max_hz),
            "--freq-count",
            str(args.freq_count),
            "--freq-spacing",
            str(args.freq_spacing),
            "--plot-theme",
            str(args.plot_theme),
            "--polar-distance-m",
            str(args.polar_distance_m),
            "--polar-angle-min-deg",
            str(args.polar_angle_min_deg),
            "--polar-angle-max-deg",
            str(args.polar_angle_max_deg),
            "--polar-angle-count",
            str(args.polar_angle_count),
            "--bem-formulation",
            str(args.bem_formulation),
            "--complex-k-shift",
            str(args.complex_k_shift),
            "--native-symmetry-plane",
            str(solve_native_symmetry_plane or "none"),
            (
                "--native-check-open-edges"
                if args.native_check_open_edges
                else "--no-native-check-open-edges"
            ),
        ]
        if args.crossover_lf_mf_hz is not None:
            solve_cmd.extend(["--crossover-lf-mf-hz", str(args.crossover_lf_mf_hz)])
        if args.crossover_mf_hf_hz is not None:
            solve_cmd.extend(["--crossover-mf-hf-hz", str(args.crossover_mf_hf_hz)])
        if args.crossover_lf_hf_hz is not None:
            solve_cmd.extend(["--crossover-lf-hf-hz", str(args.crossover_lf_hf_hz)])
        if args.source_motion is not None:
            solve_cmd.extend(["--source-motion", str(args.source_motion)])
        _extend_option_value(solve_cmd, "--frame-axis", frame_axis)
        _extend_option_value(solve_cmd, "--frame-origin", frame_origin)
        _extend_option_value(solve_cmd, "--frame-u", frame_u)
        _extend_option_value(solve_cmd, "--frame-v", frame_v)
        if args.export_vituixcad:
            solve_cmd.append("--export-vituixcad")
        for option, attr in OUTPUT_SKIP_FORWARD_OPTIONS:
            if getattr(args, attr):
                solve_cmd.append(option)
        for option, attr in DRIVER_LEM_REPEATABLE_FORWARD_OPTIONS:
            for value in getattr(args, attr):
                solve_cmd.extend([option, str(value)])
        for option, attr in DRIVER_LEM_VALUE_FORWARD_OPTIONS:
            value = getattr(args, attr)
            if value is not None:
                solve_cmd.extend([option, str(value)])
        if args.passive_cardioid_mf:
            solve_cmd.append("--passive-cardioid-mf")
            if args.passive_cardioid_rear_volume_l is not None:
                solve_cmd.extend(
                    [
                        "--passive-cardioid-rear-volume-l",
                        str(args.passive_cardioid_rear_volume_l),
                    ]
                )
            if args.passive_cardioid_port_length_mm is not None:
                solve_cmd.extend(
                    [
                        "--passive-cardioid-port-length-mm",
                        str(args.passive_cardioid_port_length_mm),
                    ]
                )
            if args.passive_cardioid_port_area_cm2 is not None:
                solve_cmd.extend(
                    [
                        "--passive-cardioid-port-area-cm2",
                        str(args.passive_cardioid_port_area_cm2),
                    ]
                )
            solve_cmd.extend(
                [
                    "--passive-cardioid-foam-resistance-pa-s-m3",
                    str(args.passive_cardioid_foam_resistance_pa_s_m3),
                ]
            )
            solve_cmd.append(
                "--passive-cardioid-invert-port"
                if args.passive_cardioid_invert_port
                else "--no-passive-cardioid-invert-port"
            )
            if args.passive_cardioid_coupled:
                solve_cmd.append("--passive-cardioid-coupled")
                for option, attr in PASSIVE_CARDIOID_COUPLED_FORWARD_OPTIONS:
                    value = getattr(args, attr)
                    if value is not None:
                        solve_cmd.extend([option, str(value)])
        if fem_mesh_path is not None:
            solve_cmd.extend(
                [
                    "--fem-chamber-mesh",
                    str(fem_mesh_path),
                    "--fem-driver-boundary",
                    str(args.fem_driver_boundary),
                    "--fem-output-source",
                    str(args.fem_output_source),
                    "--fem-output-tag",
                    str(args.fem_output_tag),
                    "--fem-loss-factor",
                    str(args.fem_loss_factor),
                    "--fem-area-tolerance",
                    str(args.fem_area_tolerance),
                ]
            )
            for name in fem_entries:
                solve_cmd.extend(["--fem-entry", name])
        for source in solve_sources:
            solve_cmd.extend(["--source", source])
        for name, freq_max_hz in solve_source_freq_max.items():
            solve_cmd.extend(["--source-freq-max", f"{name}:{freq_max_hz}"])
        # Overlay both mesh-valid bands on each source's plots so the
        # trustworthy band stays visible when solving the full band (warn):
        # the conservative fully-resolved limit (solid) and the radiating
        # aperture limit (dashed). The band between them is where only the
        # aperture is resolved. Suppressed when the add-in unchecks the
        # mesh-valid marker overlay (--no-mesh-valid-markers); the solve band
        # and the authoritative mesh-valid records above are unaffected.
        if args.mesh_valid_markers:
            effective_valid = _per_source_max_valid_hz(prep_manifest, solve_sources)
            radiating_valid = _per_source_radiating_valid_hz(prep_manifest, solve_sources)
            solve_overlay: dict[str, float] = {}
            solve_overlay_aperture: dict[str, float] = {}
            for source in solve_sources:
                name, _ = _source_name_tag(source)
                eff = effective_valid.get(name)
                rad = radiating_valid.get(name)
                if eff is not None and eff < solve_freq_max_hz:
                    solve_overlay[name] = eff
                    solve_cmd.extend(["--source-mesh-valid-hz", f"{name}:{eff}"])
                if rad is not None and rad < solve_freq_max_hz:
                    solve_overlay_aperture[name] = rad
                    solve_cmd.extend(["--source-aperture-valid-hz", f"{name}:{rad}"])
            if solve_overlay or solve_overlay_aperture:
                pipeline_manifest.setdefault("solve_frequency_adjustment", {})
                if isinstance(pipeline_manifest["solve_frequency_adjustment"], dict):
                    pipeline_manifest["solve_frequency_adjustment"][
                        "mesh_valid_overlay_freq_max_hz"
                    ] = {k: float(v) for k, v in solve_overlay.items()}
                    pipeline_manifest["solve_frequency_adjustment"][
                        "radiating_mesh_valid_freq_max_hz"
                    ] = {k: float(v) for k, v in solve_overlay_aperture.items()}
        pipeline_manifest["status"] = "solving"
        pipeline_manifest["commands"]["solve"] = solve_cmd
        pipeline_manifest["logs"].update(
            {
                "solve_stdout": str(logs_dir / "solve_fusion_wg_metal.stdout.log"),
                "solve_stderr": str(logs_dir / "solve_fusion_wg_metal.stderr.log"),
            }
        )
        pipeline_manifest["direct_solve_manifest"] = str(solve_manifest_path)
        pipeline_manifest["solve_sources"] = solve_sources
        _write_json(pipeline_manifest_path, pipeline_manifest)
        solve_returncode = _run_logged(
            solve_cmd,
            cwd=REPO_ROOT,
            stdout_path=logs_dir / "solve_fusion_wg_metal.stdout.log",
            stderr_path=logs_dir / "solve_fusion_wg_metal.stderr.log",
        )
        if solve_returncode != 0:
            pipeline_manifest["status"] = "failed"
            pipeline_manifest["error"] = "solve_fusion_wg_metal.py failed"
            pipeline_manifest["returncode"] = solve_returncode
            _write_json(pipeline_manifest_path, pipeline_manifest)
            _open_requested_outputs(args, out_dir)
            return solve_returncode

        solve_manifest = _read_json(solve_manifest_path)
        pipeline_manifest["direct_solve"] = solve_manifest

    pipeline_manifest["status"] = "complete"
    pipeline_manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if not args.no_run_report:
        pipeline_manifest["report_html"] = str(out_dir / "report.html")
    _write_json(pipeline_manifest_path, pipeline_manifest)
    _write_json(final_summary_manifest_path, pipeline_manifest)

    if not args.no_run_report:
        report_result = _render_run_report(out_dir, python=args.python)
        if report_result is not None:
            report_returncode, report_cmd = report_result
            pipeline_manifest["commands"]["report"] = report_cmd
            pipeline_manifest["report"] = {
                "path": str(out_dir / "report.html"),
                "returncode": report_returncode,
                "status": "complete" if report_returncode == 0 else "failed",
            }
            _write_json(pipeline_manifest_path, pipeline_manifest)
            _write_json(final_summary_manifest_path, pipeline_manifest)

    _open_requested_outputs(args, out_dir)
    if args.open_wg:
        _launch_waveguide_generator(wg_dir, pipeline_manifest_path)

    print(json.dumps(pipeline_manifest, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        returncode = _run_pipeline(args)
    except SystemExit:
        raise
    except Exception as exc:
        _finalize_run(
            args,
            returncode=1,
            crash_error=f"{type(exc).__name__}: {exc}",
        )
        raise
    _finalize_run(args, returncode=returncode, crash_error=None)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
