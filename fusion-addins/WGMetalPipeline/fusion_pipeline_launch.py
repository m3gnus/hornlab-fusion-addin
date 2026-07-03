"""Launch metadata helpers for the WG Metal Fusion add-in.

This module intentionally has no Fusion API imports so command construction and
launch metadata can be checked outside Fusion.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

MIRROR_PLANE_SYMMETRY_PLANES = {
    "Auto detect": "auto",
    "Left/Right + Front/Back": "x0,y0",
    "Left/Right": "x0",
    "Front/Back": "y0",
    "Top/Bottom": "z0",
    "Full model": "none",
}

# Mirror the conservative mesh-validity rule in prepare_step_for_wg_metal.py.
SPEED_OF_SOUND_M_S = 343.0
FREQUENCY_ELEMENTS_PER_WAVELENGTH = 6.0


class DriverLemParseError(ValueError):
    """Raised when a Driver LEM T/S specification cannot be parsed."""


class DriverLemSpec:
    """Parsed driver LEM input with SI-normalized values.

    Input values follow Hornresp's driver-file units: Sd cm2, Mmd/Mms g,
    Cms m/N, Le/Le2 mH, Rms kg/s, Xmax mm, Vas L.
    """

    def __init__(
        self,
        *,
        name: str,
        params: dict[str, float | int],
        source: str,
        warnings: tuple[str, ...] = (),
        ignored_keys: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.params = params
        self.source = source
        self.warnings = warnings
        self.ignored_keys = ignored_keys

    def canonical_payload(self) -> str:
        pieces: list[tuple[str, float | int]] = [
            ("Sd", float(self.params["sd_m2"]) * 1.0e4),
            ("Bl", float(self.params["bl_tm"])),
            ("Re", float(self.params["re_ohm"])),
        ]
        if "le_h" in self.params:
            pieces.append(("Le", float(self.params["le_h"]) * 1.0e3))
        if "le2_h" in self.params:
            pieces.append(("Le2", float(self.params["le2_h"]) * 1.0e3))
        if "re2_ohm" in self.params:
            pieces.append(("Re2", float(self.params["re2_ohm"])))
        if "mmd_kg" in self.params:
            pieces.append(("Mmd", float(self.params["mmd_kg"]) * 1.0e3))
        if "mms_kg" in self.params:
            pieces.append(("Mms", float(self.params["mms_kg"]) * 1.0e3))
        if "cms_m_per_n" in self.params:
            pieces.append(("Cms", float(self.params["cms_m_per_n"])))
        if "vas_m3" in self.params:
            pieces.append(("Vas", float(self.params["vas_m3"]) * 1.0e3))
        if "fs_hz" in self.params:
            pieces.append(("Fs", float(self.params["fs_hz"])))
        if "rms_kg_per_s" in self.params:
            pieces.append(("Rms", float(self.params["rms_kg_per_s"])))
        if "qms" in self.params:
            pieces.append(("Qms", float(self.params["qms"])))
        if "xmax_m" in self.params:
            pieces.append(("Xmax", float(self.params["xmax_m"]) * 1.0e3))
        if "n_drivers" in self.params:
            pieces.append(("N", int(self.params["n_drivers"])))
        return ",".join(f"{key}={value:g}" for key, value in pieces)

    def cli_entry(self) -> str:
        return f"{self.name}:{self.canonical_payload()}"


_DRIVER_KEY_MAP: dict[str, tuple[str, float, str]] = {
    "sd": ("sd_m2", 1.0e-4, "Sd"),
    "bl": ("bl_tm", 1.0, "Bl"),
    "re": ("re_ohm", 1.0, "Re"),
    "le": ("le_h", 1.0e-3, "Le"),
    "le2": ("le2_h", 1.0e-3, "Le2"),
    "re2": ("re2_ohm", 1.0, "Re2"),
    "mmd": ("mmd_kg", 1.0e-3, "Mmd"),
    "mms": ("mms_kg", 1.0e-3, "Mms"),
    "cms": ("cms_m_per_n", 1.0, "Cms"),
    "vas": ("vas_m3", 1.0e-3, "Vas"),
    "fs": ("fs_hz", 1.0, "Fs"),
    "rms": ("rms_kg_per_s", 1.0, "Rms"),
    "qms": ("qms", 1.0, "Qms"),
    "xmax": ("xmax_m", 1.0e-3, "Xmax"),
    "n": ("n_drivers", 1.0, "N"),
    "nd": ("n_drivers", 1.0, "Nd"),
}
_WARN_IGNORED_DRIVER_KEYS = {"leb", "ke", "rss"}
_WARN_IGNORED_DRIVER_PREFIXES = ("vrc",)


def _normalize_driver_key(raw: str) -> str:
    return raw.strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _driver_pairs_from_text(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.split(r"\s+#|\s+//", line, maxsplit=1)[0].strip()
        if "=" not in line:
            continue
        for token in re.split(r"[,;]+", line):
            token = token.strip()
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            pairs.append((key.strip(), value.strip()))
    return pairs


def _driver_payload_text(raw_payload: str) -> tuple[str, str]:
    payload = str(raw_payload).strip()
    if not payload:
        raise DriverLemParseError("driver T/S spec is blank")
    if "=" in payload:
        return payload, "text"
    path = Path(payload).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace"), str(path)
    raise DriverLemParseError(
        f"driver T/S spec is neither Key=Value text nor a readable file: {payload}"
    )


def parse_driver_lem_spec(name: str, raw_payload: str) -> DriverLemSpec:
    driver_name = str(name).strip()
    if not driver_name:
        raise DriverLemParseError("driver LEM entry needs a source name")
    text, source = _driver_payload_text(raw_payload)
    params: dict[str, float | int] = {}
    warnings: list[str] = []
    ignored: list[str] = []
    seen_keys: dict[str, str] = {}
    for raw_key, raw_value in _driver_pairs_from_text(text):
        key = _normalize_driver_key(raw_key)
        if key in _WARN_IGNORED_DRIVER_KEYS or any(
            key.startswith(prefix) for prefix in _WARN_IGNORED_DRIVER_PREFIXES
        ):
            ignored.append(raw_key)
            warnings.append(
                f"{driver_name}: ignoring unsupported Hornresp/system key {raw_key}"
            )
            continue
        mapped = _DRIVER_KEY_MAP.get(key)
        if mapped is None:
            ignored.append(raw_key)
            continue
        attr, scale, canonical = mapped
        if attr in seen_keys:
            try:
                duplicate = float(raw_value)
                previous = float(seen_keys[attr])
            except ValueError:
                duplicate = math.nan
                previous = math.nan
            if not math.isclose(duplicate, previous, rel_tol=1.0e-12, abs_tol=1.0e-12):
                warnings.append(
                    f"{driver_name}: duplicate {canonical}={raw_value} ignored; "
                    f"using first value {seen_keys[attr]}"
                )
            continue
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise DriverLemParseError(
                f"{driver_name}: {raw_key} must be numeric, got {raw_value!r}"
            ) from exc
        if not math.isfinite(value):
            raise DriverLemParseError(f"{driver_name}: {raw_key} must be finite")
        if attr == "n_drivers":
            if value <= 0 or not float(value).is_integer():
                raise DriverLemParseError(f"{driver_name}: {raw_key} must be a positive integer")
            params[attr] = int(value)
        else:
            if attr not in {"le_h"} and value <= 0.0:
                raise DriverLemParseError(f"{driver_name}: {raw_key} must be positive")
            if attr == "le_h" and value < 0.0:
                raise DriverLemParseError(f"{driver_name}: {raw_key} must be non-negative")
            params[attr] = value * scale
        seen_keys[attr] = raw_value

    for attr, label in (
        ("sd_m2", "Sd"),
        ("bl_tm", "Bl"),
        ("re_ohm", "Re"),
    ):
        if attr not in params:
            raise DriverLemParseError(f"{driver_name}: missing required {label}")
    if "le_h" not in params:
        params["le_h"] = 0.0
    if ("le2_h" in params) != ("re2_ohm" in params):
        raise DriverLemParseError(f"{driver_name}: Le2 and Re2 must be provided together")
    if "mmd_kg" not in params and "mms_kg" not in params:
        raise DriverLemParseError(f"{driver_name}: missing required Mmd or Mms")
    if "mmd_kg" in params and "mms_kg" in params:
        warnings.append(f"{driver_name}: both Mmd and Mms supplied; Mmd is preferred")
    if not any(key in params for key in ("cms_m_per_n", "vas_m3", "fs_hz")):
        raise DriverLemParseError(f"{driver_name}: missing required Cms, Vas, or Fs")
    if "rms_kg_per_s" in params and "qms" in params:
        warnings.append(f"{driver_name}: both Rms and Qms supplied; Rms is preferred")
    if "n_drivers" not in params:
        params["n_drivers"] = 1
    return DriverLemSpec(
        name=driver_name,
        params=params,
        source=source,
        warnings=tuple(warnings),
        ignored_keys=tuple(ignored),
    )


def parse_driver_lem_cli_entry(raw_entry: str) -> DriverLemSpec:
    entry = str(raw_entry).strip()
    if ":" not in entry:
        raise DriverLemParseError("--driver-lem expects NAME:Key=Value or NAME:/path/to/file")
    name, payload = entry.split(":", 1)
    return parse_driver_lem_spec(name, payload)


def parse_driver_lem_cli_entries(raw_entries: list[str] | tuple[str, ...]) -> dict[str, DriverLemSpec]:
    specs: dict[str, DriverLemSpec] = {}
    for raw in raw_entries:
        spec = parse_driver_lem_cli_entry(raw)
        key = spec.name.strip().upper()
        if key in specs:
            raise DriverLemParseError(f"duplicate --driver-lem entry for {spec.name}")
        specs[key] = spec
    return specs


def build_driver_lem_cli_entry(name: str, raw_payload: str | None) -> str | None:
    if raw_payload is None or not str(raw_payload).strip():
        return None
    return parse_driver_lem_spec(name, str(raw_payload)).cli_entry()


def _append_optional_cli_value(cmd: list[str], flag: str, value: str | None) -> None:
    if value and str(value).strip():
        cmd.extend([flag, str(value).strip()])


def validate_output_options(
    *,
    export_vituixcad: bool,
    output_combined_set: bool,
    crossover_lf_mf_hz: str | None = None,
    crossover_mf_hf_hz: str | None = None,
) -> None:
    has_crossover = bool(str(crossover_lf_mf_hz or "").strip()) or bool(
        str(crossover_mf_hf_hz or "").strip()
    )
    if export_vituixcad and not output_combined_set and has_crossover:
        raise ValueError(
            "VituixCAD export with crossover frequencies requires the "
            "Combined/crossover output set. Enable that output or clear the "
            "crossover frequencies."
        )


def estimate_clamped_solve_band(
    *,
    sources: str,
    rigid_res_mm: str,
    freq_max_hz: str,
) -> dict[str, float] | None:
    """Predict per-source solve ceilings for the clamp-per-source policy.

    The pipeline limits each source to ``c / (validation_epw * max_edge)`` over
    the source patch and the rigid walls within the refinement transition. In
    manual-mm sizing the source patch and shadow background are their explicit
    millimetre values, so the effective ceiling is based on the coarser of the
    source patch and rigid background.
    Returns ``{source_name: ceiling_hz}`` for sources expected to clamp below
    the requested maximum, or None when nothing clamps (or inputs do not parse;
    the pipeline validates the actual mesh authoritatively).
    """
    try:
        requested_hz = float(freq_max_hz)
        resolutions = {}
        for spec in sources.split(","):
            parts = [part.strip() for part in spec.split(":")]
            if len(parts) < 2 or not parts[0]:
                return None
            resolutions[parts[0]] = float(parts[1])
        if not resolutions:
            return None
        rigid_str = rigid_res_mm if isinstance(rigid_res_mm, str) else str(rigid_res_mm)
        rigid_mm = float(rigid_str) if rigid_str.strip() else max(resolutions.values())
    except (TypeError, ValueError):
        return None
    if requested_hz <= 0.0 or rigid_mm <= 0.0 or any(res <= 0.0 for res in resolutions.values()):
        return None
    sizing = _load_sizing()
    shadow_size = sizing.role_size_mm(
        sizing.ROLE_SHADOW,
        mm_knob_mm=rigid_mm,
    )
    clamped: dict[str, float] = {}
    for name, res_mm in resolutions.items():
        patch_size = sizing.role_size_mm(
            sizing.ROLE_RADIATING,
            mm_knob_mm=res_mm,
        )
        # The wave a source launches traverses near-walls coarsening to the
        # shadow background, so the effective limit is the coarser of the two.
        ceiling_hz = sizing.valid_f_max_hz(max(patch_size, shadow_size))
        if ceiling_hz < requested_hz:
            clamped[name] = ceiling_hz
    return clamped or None


def _load_sizing():
    """Import the shared pure-Python sizing helper from the repo's scripts/."""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import wg_mesh_sizing  # noqa: E402

    return wg_mesh_sizing


def estimate_design_mesh_cost(
    faces: list[dict[str, Any]],
    *,
    source_res_mm: dict[str, float],
    transition_mm: float,
    rigid_res_mm: float,
    freq_count: int = 1,
) -> dict[str, Any]:
    """Predict triangles/RAM/solve cost for the dialog from sampled BRep faces.

    ``faces`` is a list of ``{"area_mm2", "centroid": (x, y, z), "source_name"}``
    sampled from the active (quarter) design. Mirrors the prepare-step sizing:
    source patches at their per-source mm dial, with the near-field graded from
    that source size to the rigid shadow background.
    This is an approximate live preview; the prepare-step manifest holds the
    authoritative prediction.
    """
    sizing = _load_sizing()
    shadow_res = sizing.role_size_mm(
        sizing.ROLE_SHADOW,
        mm_knob_mm=rigid_res_mm,
    )
    # One grading site per source face: the mesher grades each source's
    # Distance/Threshold field from that source's own patch size, combined by
    # Min across sources, so the estimate mirrors that instead of grading
    # every wall from a single global near-field floor.
    source_sites = []
    for face in faces:
        name = face.get("source_name")
        if not name:
            continue
        knob = float(source_res_mm.get(name, rigid_res_mm))
        patch_size = sizing.role_size_mm(
            sizing.ROLE_RADIATING,
            mm_knob_mm=knob,
        )
        source_sites.append((tuple(face["centroid"]), float(patch_size)))

    regions = []
    for face in faces:
        area = float(face.get("area_mm2", 0.0))
        if area <= 0.0:
            continue
        name = face.get("source_name")
        if name:
            knob = float(source_res_mm.get(name, rigid_res_mm))
            size = sizing.role_size_mm(
                sizing.ROLE_RADIATING,
                mm_knob_mm=knob,
            )
            regions.append(sizing.Region(area_mm2=area, size_mm=size, label=sizing.ROLE_RADIATING))
            continue
        size = shadow_res
        cx, cy, cz = face["centroid"]
        for (sx, sy, sz), patch_size in source_sites:
            distance = ((cx - sx) ** 2 + (cy - sy) ** 2 + (cz - sz) ** 2) ** 0.5
            size = min(
                size,
                sizing.graded_size_mm(
                    distance,
                    size_min_mm=patch_size,
                    size_max_mm=shadow_res,
                    dist_max_mm=float(transition_mm),
                ),
            )
        regions.append(sizing.Region(area_mm2=area, size_mm=size, label=sizing.ROLE_NEAR_FIELD))

    return sizing.estimate_mesh_cost(regions, freq_count=freq_count).to_dict()


def format_mesh_cost_summary(estimate: dict[str, Any]) -> str:
    """One short multi-line readout of an estimate for the dialog text box."""
    per_role = estimate.get("per_role_triangles", {})
    role_bits = ", ".join(f"{role} {count:,}" for role, count in sorted(per_role.items()))
    valid = estimate.get("per_role_valid_f_max_hz", {})
    radiating_valid = valid.get("radiating")
    feasibility = estimate.get("feasibility", "ok")
    flag = {"ok": "", "caution": " (caution)", "warn": " (WARNING)", "infeasible": " (INFEASIBLE)"}.get(
        feasibility, ""
    )
    lines = [
        f"~{estimate.get('n_triangles', 0):,} triangles ({role_bits})" if role_bits
        else f"~{estimate.get('n_triangles', 0):,} triangles",
        f"BEM matrix RAM ~{estimate.get('ram_gb', 0.0):.1f} GB{flag}",
        f"solve ~{estimate.get('solve_seconds_per_freq', 0.0):.1f} s/freq",
    ]
    if radiating_valid:
        lines.append(f"radiating valid to ~{radiating_valid:.0f} Hz")
    return "\n".join(lines)


def build_source_specs(
    *,
    lf_mesh_mm: str,
    mf_mesh_mm: str,
    hf_mesh_mm: str,
    port_exit_mesh_mm: str = "",
    port_exit_l_mesh_mm: str = "",
    port_exit_r_mesh_mm: str = "",
) -> str:
    sources = []
    for name, value in (
        ("LF", lf_mesh_mm),
        ("MF", mf_mesh_mm),
        ("HF", hf_mesh_mm),
    ):
        resolution = value.strip()
        if resolution:
            sources.append(f"{name}:{resolution}")
    port_exit_resolution = port_exit_mesh_mm.strip()
    if port_exit_resolution:
        if port_exit_l_mesh_mm.strip() or port_exit_r_mesh_mm.strip():
            raise ValueError("PORT_EXIT cannot be combined with PORT_EXIT_L/PORT_EXIT_R")
        sources.append(f"PORT_EXIT:{port_exit_resolution}:10")
    for name, value, tag in (
        ("PORT_EXIT_L", port_exit_l_mesh_mm, 10),
        ("PORT_EXIT_R", port_exit_r_mesh_mm, 11),
    ):
        resolution = value.strip()
        if resolution:
            sources.append(f"{name}:{resolution}:{tag}")
    return ",".join(sources)


def build_pipeline_command(
    *,
    python_path: Path,
    pipeline_script: Path,
    step_path: Path,
    out_dir: Path,
    sources: str,
    transition_mm: str,
    rigid_res_mm: str | None,
    freq_min_hz: str,
    freq_max_hz: str,
    freq_count: str,
    freq_spacing: str,
    polar_distance_m: str,
    polar_angle_min_deg: str,
    polar_angle_max_deg: str,
    polar_angle_count: str,
    wg_dir: Path,
    mesh_only: bool,
    open_wg: bool,
    open_output: bool,
    open_report: bool = False,
    plot_theme: str = "hornlab",
    crossover_lf_mf_hz: str | None = None,
    crossover_mf_hf_hz: str | None = None,
    symmetry_planes: str = "auto",
    quadrants: str | None = None,
    mirror_axes: str | None = None,
    refine: list[str] | None = None,
    skip_missing_sources: bool = True,
    allow_underresolved_solve: bool = False,
    underresolved_solve_policy: str = "warn",
    show_mesh_valid_markers: bool = True,
    export_vituixcad: bool = False,
    output_per_driver_plots: bool = True,
    output_combined_set: bool = True,
    output_passive_cardioid_set: bool = True,
    output_driver_lem_artifacts: bool = True,
    output_derived_acoustics: bool = True,
    output_radiation_impedance: bool = True,
    output_pressure_bases: bool = True,
    output_run_report: bool = True,
    notify: bool = True,
    bem_formulation: str = "complex_k",
    complex_k_shift: str = "0.005",
    passive_cardioid_enabled: bool = False,
    passive_cardioid_rear_volume_l: str | None = None,
    passive_cardioid_port_length_mm: str | None = None,
    passive_cardioid_port_area_cm2: str | None = None,
    passive_cardioid_foam_resistance_pa_s_m3: str | None = None,
    passive_cardioid_invert_port: bool = True,
    passive_cardioid_coupled: bool = False,
    passive_cardioid_driver_sd_cm2: str | None = None,
    passive_cardioid_driver_bl_tm: str | None = None,
    passive_cardioid_driver_re_ohm: str | None = None,
    passive_cardioid_driver_le_mh: str | None = None,
    passive_cardioid_driver_mms_g: str | None = None,
    passive_cardioid_driver_cms_mm_per_n: str | None = None,
    passive_cardioid_driver_vas_l: str | None = None,
    passive_cardioid_driver_fs_hz: str | None = None,
    passive_cardioid_driver_qms: str | None = None,
    passive_cardioid_drive_voltage: str | None = None,
    passive_cardioid_rg_ohm: str | None = None,
    lf_driver_lem: str | None = None,
    mf_driver_lem: str | None = None,
    hf_driver_lem: str | None = None,
    lf_driver_rear_volume_l: str | None = None,
    mf_driver_rear_volume_l: str | None = None,
    hf_driver_rear_volume_l: str | None = None,
    drive_voltage: str | None = None,
    rg_ohm: str | None = None,
) -> list[str]:
    validate_output_options(
        export_vituixcad=export_vituixcad,
        output_combined_set=output_combined_set,
        crossover_lf_mf_hz=crossover_lf_mf_hz,
        crossover_mf_hf_hz=crossover_mf_hf_hz,
    )
    plot_theme_value = str(plot_theme or "").strip() or "hornlab"
    cmd = [
        str(python_path),
        str(pipeline_script),
        "--step",
        str(step_path),
        "--out",
        str(out_dir),
        "--sources",
        sources,
        "--transition-mm",
        transition_mm,
    ]
    if rigid_res_mm and rigid_res_mm.strip():
        cmd.extend(["--rigid-res-mm", rigid_res_mm.strip()])
    for entry in refine or []:
        entry = str(entry).strip()
        if entry:
            cmd.extend(["--refine", entry])
    symmetry_auto = symmetry_planes.strip().lower() == "auto"
    if not symmetry_auto and quadrants:
        cmd.extend(["--quadrants", quadrants])
    cmd.extend(["--symmetry-planes", "auto" if symmetry_auto else symmetry_planes])
    if not symmetry_auto and mirror_axes:
        cmd.extend(["--mirror-axes", mirror_axes])
    cmd.extend([
        "--freq-min-hz",
        freq_min_hz,
        "--freq-max-hz",
        freq_max_hz,
        "--freq-count",
        freq_count,
        "--freq-spacing",
        freq_spacing,
        "--plot-theme",
        plot_theme_value,
        "--polar-distance-m",
        polar_distance_m,
        "--polar-angle-min-deg",
        polar_angle_min_deg,
        "--polar-angle-max-deg",
        polar_angle_max_deg,
        "--polar-angle-count",
        polar_angle_count,
        "--bem-formulation",
        bem_formulation,
        "--complex-k-shift",
        complex_k_shift,
        "--wg-dir",
        str(wg_dir),
    ])
    if crossover_lf_mf_hz and str(crossover_lf_mf_hz).strip():
        cmd.extend(["--crossover-lf-mf-hz", str(crossover_lf_mf_hz).strip()])
    if crossover_mf_hf_hz and str(crossover_mf_hf_hz).strip():
        cmd.extend(["--crossover-mf-hf-hz", str(crossover_mf_hf_hz).strip()])
    if mesh_only:
        cmd.append("--mesh-only")
    else:
        cmd.append("--run-solves")
    if open_wg:
        cmd.append("--open-wg")
    if open_output:
        cmd.append("--open-output-folder")
    if open_report:
        cmd.append("--open-report")
    if skip_missing_sources:
        cmd.append("--skip-missing-sources")
    if allow_underresolved_solve:
        cmd.append("--allow-underresolved-solve")
    else:
        cmd.extend(["--underresolved-solve-policy", underresolved_solve_policy])
    if not show_mesh_valid_markers:
        cmd.append("--no-mesh-valid-markers")
    if export_vituixcad:
        cmd.append("--export-vituixcad")
    if not output_per_driver_plots:
        cmd.append("--skip-per-driver-plots")
    if not output_combined_set:
        cmd.append("--skip-combined-set")
    if not output_passive_cardioid_set:
        cmd.append("--skip-passive-cardioid-set")
    if not output_driver_lem_artifacts:
        cmd.append("--skip-driver-lem-artifacts")
    if not output_derived_acoustics:
        cmd.append("--skip-derived-acoustics")
    if not output_radiation_impedance:
        cmd.append("--skip-radiation-impedance")
    if not output_pressure_bases:
        cmd.append("--skip-pressure-bases")
    if not output_run_report:
        cmd.append("--no-run-report")
    for name, raw_payload in (
        ("LF", lf_driver_lem),
        ("MF", mf_driver_lem),
        ("HF", hf_driver_lem),
    ):
        entry = build_driver_lem_cli_entry(name, raw_payload)
        if entry is not None:
            cmd.extend(["--driver-lem", entry])
    for name, raw_volume in (
        ("LF", lf_driver_rear_volume_l),
        ("MF", mf_driver_rear_volume_l),
        ("HF", hf_driver_rear_volume_l),
    ):
        if raw_volume and str(raw_volume).strip():
            cmd.extend(["--driver-rear-volume-l", f"{name}:{str(raw_volume).strip()}"])
    _append_optional_cli_value(cmd, "--drive-voltage", drive_voltage)
    _append_optional_cli_value(cmd, "--rg-ohm", rg_ohm)
    if passive_cardioid_enabled:
        cmd.append("--passive-cardioid-mf")
        if passive_cardioid_rear_volume_l and str(passive_cardioid_rear_volume_l).strip():
            cmd.extend(
                [
                    "--passive-cardioid-rear-volume-l",
                    str(passive_cardioid_rear_volume_l).strip(),
                ]
            )
        if passive_cardioid_port_length_mm and str(passive_cardioid_port_length_mm).strip():
            cmd.extend(
                [
                    "--passive-cardioid-port-length-mm",
                    str(passive_cardioid_port_length_mm).strip(),
                ]
            )
        if passive_cardioid_port_area_cm2 and str(passive_cardioid_port_area_cm2).strip():
            cmd.extend(
                [
                    "--passive-cardioid-port-area-cm2",
                    str(passive_cardioid_port_area_cm2).strip(),
                ]
            )
        if (
            passive_cardioid_foam_resistance_pa_s_m3
            and str(passive_cardioid_foam_resistance_pa_s_m3).strip()
        ):
            cmd.extend(
                [
                    "--passive-cardioid-foam-resistance-pa-s-m3",
                    str(passive_cardioid_foam_resistance_pa_s_m3).strip(),
                ]
            )
        cmd.append(
            "--passive-cardioid-invert-port"
            if passive_cardioid_invert_port
            else "--no-passive-cardioid-invert-port"
        )
        if passive_cardioid_coupled:
            cmd.append("--passive-cardioid-coupled")
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-sd-cm2",
                passive_cardioid_driver_sd_cm2,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-bl-tm",
                passive_cardioid_driver_bl_tm,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-re-ohm",
                passive_cardioid_driver_re_ohm,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-le-mh",
                passive_cardioid_driver_le_mh,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-mms-g",
                passive_cardioid_driver_mms_g,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-cms-mm-per-n",
                passive_cardioid_driver_cms_mm_per_n,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-vas-l",
                passive_cardioid_driver_vas_l,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-fs-hz",
                passive_cardioid_driver_fs_hz,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-driver-qms",
                passive_cardioid_driver_qms,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-drive-voltage",
                passive_cardioid_drive_voltage,
            )
            _append_optional_cli_value(
                cmd,
                "--passive-cardioid-rg-ohm",
                passive_cardioid_rg_ohm,
            )
    if notify:
        cmd.append("--notify")
    return cmd


def symmetry_planes_for_mirror_plane(mirror_plane: str) -> str:
    return MIRROR_PLANE_SYMMETRY_PLANES.get(mirror_plane, mirror_plane)


def quadrants_for_symmetry_planes(symmetry_planes: str) -> str:
    if symmetry_planes.strip().lower() == "auto":
        return "auto"
    planes = {
        part.strip().lower()
        for part in symmetry_planes.split(",")
        if part.strip().lower() not in {"none", "full", "full model"}
    }
    if planes == {"x0", "y0"}:
        return "1"
    if planes == {"y0"}:
        return "12"
    if planes == {"x0"}:
        return "14"
    return "1234"


def mirror_axes_for_symmetry_planes(symmetry_planes: str) -> str:
    if symmetry_planes.strip().lower() == "auto":
        return "auto"
    axes = []
    for part in symmetry_planes.split(","):
        plane = part.strip().lower()
        if plane in {"x0", "y0", "z0"}:
            axes.append(plane[0])
    return ",".join(axes) if axes else "none"


def expected_pipeline_paths(out_dir: Path) -> dict[str, Any]:
    logs_dir = out_dir / "logs"
    sources_dir = out_dir / "sources"
    combined_dir = out_dir / "combined"
    return {
        "logs_dir": str(logs_dir),
        "launcher_stdout": str(logs_dir / "fusion_step_to_wg_pipeline.stdout.log"),
        "launcher_stderr": str(logs_dir / "fusion_step_to_wg_pipeline.stderr.log"),
        "prepare_stdout": str(logs_dir / "prepare_step_for_wg_metal.stdout.log"),
        "prepare_stderr": str(logs_dir / "prepare_step_for_wg_metal.stderr.log"),
        "diagnose_stdout": str(logs_dir / "diagnose_wg_metal_orientation.stdout.log"),
        "diagnose_stderr": str(logs_dir / "diagnose_wg_metal_orientation.stderr.log"),
        "solve_stdout": str(logs_dir / "solve_fusion_wg_metal.stdout.log"),
        "solve_stderr": str(logs_dir / "solve_fusion_wg_metal.stderr.log"),
        "pipeline_manifest": str(out_dir / "fusion_wg_pipeline_manifest.json"),
        "final_summary_manifest": str(out_dir / "final_summary_manifest.json"),
        "prepare_manifest": str(out_dir / "manifest.json"),
        "orientation_report": str(out_dir / "orientation_report.json"),
        "direct_solve_manifest": str(out_dir / "direct_solve_manifest.json"),
        "combined_time_aligned_frequency_response_png": str(
            combined_dir / "combined_frequency_response_time_aligned.png"
        ),
        "combined_time_aligned_directivity_heatmap_png": str(
            combined_dir / "combined_directivity_heatmap_time_aligned.png"
        ),
        "combined_interference_heatmap_png": str(
            combined_dir / "combined_interference_heatmap_time_aligned.png"
        ),
        "driver_time_alignment_txt": str(combined_dir / "driver_time_alignment.txt"),
        "vituixcad_export_dir": str(out_dir / "vituixcad"),
        "run_report_html": str(out_dir / "report.html"),
        "port_exit_radiation_impedance_npz": str(
            sources_dir / "port_exit_radiation_impedance_matrix.npz"
        ),
        "port_exit_radiation_impedance_summary": str(
            sources_dir / "port_exit_radiation_impedance_matrix.summary.json"
        ),
    }


def build_launch_metadata(
    *,
    command: list[str],
    pid: int | None,
    started_at: str,
    output_dir: Path,
    step_path: Path,
    cwd: Path,
    status: str,
    fusion_archive_path: Path | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "status": status,
        "command": command,
        "pid": pid,
        "started_at": started_at,
        "output_dir": str(output_dir),
        "step": str(step_path),
        "fusion_archive": str(fusion_archive_path) if fusion_archive_path else None,
        "cwd": str(cwd),
        "expected_paths": expected_pipeline_paths(output_dir),
        "notes": [
            "Fusion exported STEP and a native .f3d archive synchronously, "
            "then launched this process in the background.",
            "Fusion can be used while the external pipeline runs.",
            "Check fusion_wg_pipeline_manifest.json or final_summary_manifest.json for completion status.",
        ],
    }
    if error:
        metadata["error"] = error
    return metadata


def write_launch_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
