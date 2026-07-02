"""Fusion add-in for the HornLab STEP -> WG Metal BEM pipeline."""

from __future__ import annotations

import datetime as _datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

import adsk.core
import adsk.fusion

ADDIN_DIR = Path(__file__).resolve().parent
if str(ADDIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADDIN_DIR))

_HELPER_PATH = ADDIN_DIR / "fusion_pipeline_launch.py"
_HELPER_SPEC = importlib.util.spec_from_file_location(
    "_wgmetal_fusion_pipeline_launch",
    _HELPER_PATH,
)
if _HELPER_SPEC is None or _HELPER_SPEC.loader is None:
    raise RuntimeError(f"Could not load Fusion pipeline helper: {_HELPER_PATH}")
_fusion_pipeline_launch = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(_fusion_pipeline_launch)
build_launch_metadata = _fusion_pipeline_launch.build_launch_metadata
build_pipeline_command = _fusion_pipeline_launch.build_pipeline_command
build_source_specs = _fusion_pipeline_launch.build_source_specs
estimate_clamped_solve_band = _fusion_pipeline_launch.estimate_clamped_solve_band
estimate_design_mesh_cost = _fusion_pipeline_launch.estimate_design_mesh_cost
format_mesh_cost_summary = _fusion_pipeline_launch.format_mesh_cost_summary
expected_pipeline_paths = _fusion_pipeline_launch.expected_pipeline_paths
mirror_axes_for_symmetry_planes = _fusion_pipeline_launch.mirror_axes_for_symmetry_planes
quadrants_for_symmetry_planes = _fusion_pipeline_launch.quadrants_for_symmetry_planes
symmetry_planes_for_mirror_plane = _fusion_pipeline_launch.symmetry_planes_for_mirror_plane
write_launch_metadata = _fusion_pipeline_launch.write_launch_metadata


ADDIN_NAME = "WG Metal Pipeline"
CMD_ID = "hornlab_wg_metal_pipeline_export"
CMD_NAME = "Export to WG Metal"
CMD_DESCRIPTION = "Export STEP, prepare WG Metal meshes, diagnose orientation, and optionally solve directly."

# ADDIN_DIR is symlink-resolved, so the supported symlinked install (see
# scripts/install_fusion_wg_metal_addin.py --symlink) lands back inside the
# repo checkout and REPO_ROOT is valid wherever the repo was cloned.
REPO_ROOT = ADDIN_DIR.parents[1]
PIPELINE_SCRIPT = REPO_ROOT / "scripts" / "fusion_step_to_wg_pipeline.py"


def _default_python() -> Path:
    """First existing interpreter candidate; the dialog setting overrides."""
    candidates = (
        REPO_ROOT / ".venv" / "bin" / "python",
        Path.home() / ".waveguide-generator" / "opencl-cpu-env" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("/usr/bin/python3")


DEFAULT_PYTHON = _default_python()
DEFAULT_WG_DIR = REPO_ROOT.parent / "Waveguide Generator"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "runs" / "fusion360"
SETTINGS_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "HornLab"
    / "WGMetalPipeline"
    / "settings.json"
)
SETTINGS_VERSION = 12
DEFAULT_SETTINGS = {
    "settings_version": SETTINGS_VERSION,
    "output_root": str(DEFAULT_OUTPUT_ROOT),
    "lf_mesh_mm": "20",
    "mf_mesh_mm": "10",
    "hf_mesh_mm": "5",
    "port_exit_mesh_mm": "",
    "rigid_res_mm": "20",
    "refine": "",
    "freq_min_hz": "50",
    "freq_max_hz": "20000",
    "freq_count": "60",
    "freq_spacing": "log",
    "crossover_lf_mf_hz": "130",
    "crossover_mf_hf_hz": "1000",
    "polar_distance_m": "2",
    "polar_angle_min_deg": "0",
    "polar_angle_max_deg": "180",
    "polar_angle_count": "37",
    "transition_mm": "200",
    "mirror_plane": "Auto detect",
    "python_path": str(DEFAULT_PYTHON),
    "wg_dir": str(DEFAULT_WG_DIR),
    "mesh_only": False,
    "open_wg": False,
    "open_output": True,
    "export_vituixcad": False,
    "clamp_to_mesh_limit": False,
    "show_mesh_valid_markers": True,
    "passive_cardioid_enabled": False,
    "passive_cardioid_rear_volume_l": "",
    "passive_cardioid_port_length_mm": "0",
    "passive_cardioid_port_area_cm2": "",
    "passive_cardioid_foam_resistance_pa_s_m3": "0",
    "passive_cardioid_invert_port": True,
    "passive_cardioid_coupled": False,
    "lf_driver_lem": "",
    "mf_driver_lem": "",
    "hf_driver_lem": "",
    "lf_driver_rear_volume_l": "",
    "mf_driver_rear_volume_l": "",
    "hf_driver_rear_volume_l": "",
    "drive_voltage": "2.83",
    "rg_ohm": "0",
}
# Keys removed or redefined at a given settings version, mapped to the version
# that made them stale. A stored key is dropped on load only when the file
# predates that version, so a bump for one key no longer wipes the others.
_STALE_SETTINGS_KEY_VERSIONS = {
    "mirror_plane": 9,
    "quadrants": 9,
    "run_solves": 9,
    "skip_missing_sources": 9,
    "allow_underresolved_solve": 9,
    "underresolved_solve_policy": 9,
    "clamp_to_mesh_limit": 9,
    "mesh_sizing_mode": 11,
    "radiating_epw": 11,
    "shadow_epw": 11,
    "throat_epw": 11,
    "passive_cardioid_driver_sd_cm2": 12,
    "passive_cardioid_driver_bl_tm": 12,
    "passive_cardioid_driver_re_ohm": 12,
    "passive_cardioid_driver_le_mh": 12,
    "passive_cardioid_driver_le2_mh": 12,
    "passive_cardioid_driver_re2_ohm": 12,
    "passive_cardioid_driver_mmd_g": 12,
    "passive_cardioid_driver_mms_g": 12,
    "passive_cardioid_driver_cms_mm_per_n": 12,
    "passive_cardioid_driver_vas_l": 12,
    "passive_cardioid_driver_fs_hz": 12,
    "passive_cardioid_driver_rms_kg_per_s": 12,
    "passive_cardioid_driver_qms": 12,
    "passive_cardioid_driver_count": 12,
    "passive_cardioid_drive_voltage": 12,
    "passive_cardioid_rg_ohm": 12,
}
_LEGACY_PASSIVE_CARDIOID_DRIVER_FIELDS = (
    ("passive_cardioid_driver_sd_cm2", "Sd", 1.0),
    ("passive_cardioid_driver_bl_tm", "Bl", 1.0),
    ("passive_cardioid_driver_re_ohm", "Re", 1.0),
    ("passive_cardioid_driver_le_mh", "Le", 1.0),
    ("passive_cardioid_driver_le2_mh", "Le2", 1.0),
    ("passive_cardioid_driver_re2_ohm", "Re2", 1.0),
    ("passive_cardioid_driver_mmd_g", "Mmd", 1.0),
    ("passive_cardioid_driver_mms_g", "Mms", 1.0),
    ("passive_cardioid_driver_cms_mm_per_n", "Cms", 1.0e-3),
    ("passive_cardioid_driver_vas_l", "Vas", 1.0),
    ("passive_cardioid_driver_fs_hz", "Fs", 1.0),
    ("passive_cardioid_driver_rms_kg_per_s", "Rms", 1.0),
    ("passive_cardioid_driver_qms", "Qms", 1.0),
    ("passive_cardioid_driver_count", "N", 1.0),
)

_handlers = []
_control = None
_command_definition = None


def _ui():
    app = adsk.core.Application.get()
    return app.userInterface if app else None


def _show_message(message: str) -> None:
    ui = _ui()
    if ui:
        ui.messageBox(message, ADDIN_NAME)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned or "fusion_design"


def _stale_install_warning() -> str | None:
    """Warn when the add-in runs from a copied install instead of the repo.

    ADDIN_DIR is resolved through symlinks, so a symlinked install points back
    into the repo checkout and the pipeline scripts are reachable. A copied
    install has no scripts/ tree next to it and cannot launch the pipeline.
    """
    if PIPELINE_SCRIPT.exists():
        return None
    return (
        "WARNING: this add-in is running from a copied install:\n"
        f"{ADDIN_DIR}\n"
        "The pipeline scripts are not reachable from here. Clone the\n"
        "hornlab-fusion-addin repository and reinstall as a symlink:\n"
        "python3 scripts/install_fusion_wg_metal_addin.py --symlink --replace"
    )


def _input_by_id(inputs, input_id: str):
    """Find a command input by id, including inputs nested inside groups."""
    if inputs is None:
        return None
    try:
        item = inputs.itemById(input_id)
    except Exception:
        item = None
    if item is not None:
        return item
    try:
        count = int(inputs.count)
    except Exception:
        return None
    for index in range(count):
        try:
            child = inputs.item(index)
        except Exception:
            continue
        nested = getattr(child, "children", None)
        if nested is None:
            continue
        found = _input_by_id(nested, input_id)
        if found is not None:
            return found
    return None


def _input_value(inputs, input_id: str):
    item = _input_by_id(inputs, input_id)
    return item.value if item else None


def _legacy_passive_cardioid_driver_payload(loaded: dict) -> str:
    pieces: list[str] = []
    for key, canonical, scale in _LEGACY_PASSIVE_CARDIOID_DRIVER_FIELDS:
        raw = str(loaded.get(key, "")).strip()
        if not raw:
            continue
        try:
            value = float(raw) * scale
        except ValueError:
            value_text = raw
        else:
            value_text = f"{value:g}"
        pieces.append(f"{canonical}={value_text}")
    return ",".join(pieces)


def _migrate_v11_passive_cardioid_driver_settings(
    loaded: dict,
    loaded_version: int,
) -> None:
    if loaded_version >= 12 or not bool(loaded.get("passive_cardioid_coupled")):
        return
    if not str(loaded.get("mf_driver_lem", "")).strip():
        payload = _legacy_passive_cardioid_driver_payload(loaded)
        if payload:
            try:
                spec = _fusion_pipeline_launch.parse_driver_lem_spec("MF", payload)
            except _fusion_pipeline_launch.DriverLemParseError:
                loaded["passive_cardioid_coupled"] = False
            else:
                loaded["mf_driver_lem"] = spec.canonical_payload()
        else:
            loaded["passive_cardioid_coupled"] = False
    if loaded.get("passive_cardioid_coupled") is not False:
        drive_voltage = str(loaded.get("passive_cardioid_drive_voltage", "")).strip()
        rg_ohm = str(loaded.get("passive_cardioid_rg_ohm", "")).strip()
        if drive_voltage:
            loaded["drive_voltage"] = drive_voltage
        if rg_ohm:
            loaded["rg_ohm"] = rg_ohm


def _load_settings() -> dict:
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except Exception:
        return dict(DEFAULT_SETTINGS)
    if not isinstance(loaded, dict):
        return dict(DEFAULT_SETTINGS)
    try:
        loaded_version = int(loaded.get("settings_version", 1))
    except (TypeError, ValueError):
        loaded_version = 1
    _migrate_v11_passive_cardioid_driver_settings(loaded, loaded_version)
    for key, stale_version in _STALE_SETTINGS_KEY_VERSIONS.items():
        if loaded_version < stale_version:
            loaded.pop(key, None)
    settings = dict(DEFAULT_SETTINGS)
    settings.update(loaded)
    if not str(settings.get("port_exit_mesh_mm", "")).strip():
        legacy_left = str(loaded.get("port_exit_l_mesh_mm", "")).strip()
        legacy_right = str(loaded.get("port_exit_r_mesh_mm", "")).strip()
        if legacy_left and (not legacy_right or legacy_left == legacy_right):
            settings["port_exit_mesh_mm"] = legacy_left
        elif legacy_right and not legacy_left:
            settings["port_exit_mesh_mm"] = legacy_right
    settings["settings_version"] = SETTINGS_VERSION
    return settings


def _save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _setting_str(settings: dict, input_id: str) -> str:
    return str(settings.get(input_id, DEFAULT_SETTINGS.get(input_id, "")))


def _setting_bool(settings: dict, input_id: str) -> bool:
    value = settings.get(input_id, DEFAULT_SETTINGS.get(input_id, False))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _selected_dropdown_name(inputs, input_id: str) -> str:
    item = _input_by_id(inputs, input_id)
    selected = item.selectedItem if item else None
    return selected.name if selected else ""


def _active_design_name(design) -> str:
    doc = adsk.core.Application.get().activeDocument
    if doc and doc.name:
        return doc.name
    root = design.rootComponent if design else None
    return root.name if root and root.name else "fusion_design"


def _export_step(design, step_path: Path) -> None:
    export_manager = design.exportManager
    options = export_manager.createSTEPExportOptions(str(step_path))
    ok = export_manager.execute(options)
    if not ok:
        raise RuntimeError(f"Fusion STEP export failed: {step_path}")


def _export_fusion_archive(design, archive_path: Path) -> None:
    export_manager = design.exportManager
    options = export_manager.createFusionArchiveExportOptions(str(archive_path))
    if options is None:
        raise RuntimeError(f"Fusion archive export options failed: {archive_path}")
    ok = export_manager.execute(options)
    if not ok:
        raise RuntimeError(f"Fusion archive export failed: {archive_path}")


def _iter_brep_faces(design):
    """Yield every BRep face in the active design (root + all occurrences)."""
    root = design.rootComponent
    seen_bodies = set()
    body_lists = [root.bRepBodies]
    try:
        for occ in root.allOccurrences:
            comp = occ.component
            if comp is not None:
                body_lists.append(comp.bRepBodies)
    except Exception:
        pass
    for bodies in body_lists:
        for body in bodies:
            key = getattr(body, "entityToken", None) or id(body)
            if key in seen_bodies:
                continue
            seen_bodies.add(key)
            for face in body.faces:
                yield face


def _sample_design_faces(design, source_names):
    """Sample BRep face area (mm^2), centroid (mm), and source appearance.

    Fusion's internal length unit is centimetres, so areas (cm^2) scale by 100
    and points (cm) by 10 to reach the millimetre STEP units the prepare step
    works in. A face is tagged with a source when its painted appearance name
    matches a declared source.
    """
    faces_out = []
    for face in _iter_brep_faces(design):
        try:
            area_mm2 = float(face.area) * 100.0
        except Exception:
            continue
        if area_mm2 <= 0.0:
            continue
        cx = cy = cz = 0.0
        try:
            pt = face.pointOnFace
            cx, cy, cz = pt.x * 10.0, pt.y * 10.0, pt.z * 10.0
        except Exception:
            try:
                bb = face.boundingBox
                cx = (bb.minPoint.x + bb.maxPoint.x) * 0.5 * 10.0
                cy = (bb.minPoint.y + bb.maxPoint.y) * 0.5 * 10.0
                cz = (bb.minPoint.z + bb.maxPoint.z) * 0.5 * 10.0
            except Exception:
                pass
        source_name = None
        try:
            appearance = face.appearance
            label = appearance.name.lower() if appearance and appearance.name else ""
            for name in source_names:
                lowered = name.lower()
                if lowered and (lowered == label or lowered in label):
                    source_name = name
                    break
        except Exception:
            pass
        faces_out.append({"area_mm2": area_mm2, "centroid": (cx, cy, cz), "source_name": source_name})
    return faces_out


def _update_size_prediction(inputs) -> None:
    """Recompute the live triangle/RAM/cost estimate into the dialog text box."""
    box = _input_by_id(inputs, "size_prediction")
    if box is None:
        return

    def _val(input_id: str) -> str:
        item = _input_by_id(inputs, input_id)
        return str(item.value) if item and item.value is not None else ""

    try:
        design = adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct)
        if design is None:
            box.text = "estimate unavailable (no active design)"
            return
        sources_str = build_source_specs(
            lf_mesh_mm=_val("lf_mesh_mm"),
            mf_mesh_mm=_val("mf_mesh_mm"),
            hf_mesh_mm=_val("hf_mesh_mm"),
            port_exit_mesh_mm=_val("port_exit_mesh_mm"),
        )
        source_res: dict[str, float] = {}
        for token in sources_str.split(","):
            parts = [p.strip() for p in token.split(":")]
            if len(parts) >= 2 and parts[0]:
                try:
                    source_res[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        rigid_raw = _val("rigid_res_mm").strip()
        rigid_mm = float(rigid_raw) if rigid_raw else (max(source_res.values()) if source_res else 30.0)
        faces = _sample_design_faces(design, list(source_res.keys()))
        if not faces:
            box.text = "estimate unavailable (no BRep faces found)"
            return
        estimate = estimate_design_mesh_cost(
            faces,
            source_res_mm=source_res,
            transition_mm=float(_val("transition_mm") or 200.0),
            rigid_res_mm=rigid_mm,
            freq_count=int(float(_val("freq_count") or 60.0)),
        )
        box.text = format_mesh_cost_summary(estimate) + "\n(approx; exact in prepare manifest)"
    except Exception:
        box.text = "estimate unavailable (computed exactly during prepare)"


def _sync_passive_cardioid_ui(inputs) -> None:
    enabled = bool(_input_value(inputs, "passive_cardioid_enabled"))
    for input_id in (
        "passive_cardioid_rear_volume_l",
        "passive_cardioid_port_length_mm",
        "passive_cardioid_port_area_cm2",
        "passive_cardioid_foam_resistance_pa_s_m3",
        "passive_cardioid_invert_port",
        "passive_cardioid_coupled",
    ):
        item = _input_by_id(inputs, input_id)
        if item is not None:
            item.isEnabled = enabled


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            command = args.command
            inputs = command.commandInputs
            settings = _load_settings()

            sources_group = inputs.addGroupCommandInput("grp_sources", "Sources and mesh")
            sources_group.isExpanded = True
            sources = sources_group.children
            sources.addStringValueInput("lf_mesh_mm", "LF source mesh mm", _setting_str(settings, "lf_mesh_mm"))
            sources.addStringValueInput("mf_mesh_mm", "MF source mesh mm", _setting_str(settings, "mf_mesh_mm"))
            sources.addStringValueInput("hf_mesh_mm", "HF source mesh mm", _setting_str(settings, "hf_mesh_mm"))
            sources.addStringValueInput(
                "port_exit_mesh_mm",
                "Port exit mesh mm",
                _setting_str(settings, "port_exit_mesh_mm"),
            )
            sources.addStringValueInput("rigid_res_mm", "Rigid body mesh mm", _setting_str(settings, "rigid_res_mm"))
            sources.addStringValueInput("transition_mm", "Transition mm", _setting_str(settings, "transition_mm"))

            sizing_group = inputs.addGroupCommandInput("grp_sizing", "Mesh sizing")
            sizing_group.isExpanded = True
            szc = sizing_group.children
            refine_input = szc.addStringValueInput(
                "refine", "Refine overrides", _setting_str(settings, "refine")
            )
            refine_input.tooltip = (
                "Optional per-appearance overrides, comma-separated: NAME:<num>mm. "
                "Painted faces stay rigid. Example: Rim:8mm"
            )
            prediction = szc.addTextBoxCommandInput(
                "size_prediction", "Estimate", "estimate updates as dials change", 4, True
            )
            prediction.isFullWidth = True

            solve_group = inputs.addGroupCommandInput("grp_solve", "Solve")
            solve_group.isExpanded = True
            solve = solve_group.children
            solve.addStringValueInput("freq_min_hz", "Frequency min Hz", _setting_str(settings, "freq_min_hz"))
            solve.addStringValueInput("freq_max_hz", "Frequency max Hz", _setting_str(settings, "freq_max_hz"))
            solve.addStringValueInput("freq_count", "Number of frequencies", _setting_str(settings, "freq_count"))
            spacing = solve.addDropDownCommandInput(
                "freq_spacing",
                "Frequency spacing",
                adsk.core.DropDownStyles.TextListDropDownStyle,
            )
            freq_spacing = _setting_str(settings, "freq_spacing")
            spacing.listItems.add("log", freq_spacing != "linear")
            spacing.listItems.add("linear", freq_spacing == "linear")
            xo_lf_mf_input = solve.addStringValueInput(
                "crossover_lf_mf_hz",
                "LF/MF XO Hz",
                _setting_str(settings, "crossover_lf_mf_hz"),
            )
            xo_lf_mf_input.tooltip = (
                "LR4 crossover for the combined-speaker outputs. A two-way "
                "design (two of LF/MF/HF) needs just one crossover field; a "
                "three-way needs both. Blank both to skip the combine."
            )
            xo_mf_hf_input = solve.addStringValueInput(
                "crossover_mf_hf_hz",
                "MF/HF XO Hz",
                _setting_str(settings, "crossover_mf_hf_hz"),
            )
            xo_mf_hf_input.tooltip = (
                "LR4 crossover between MF (or LF in a two-way without MF) "
                "and HF. See LF/MF XO Hz."
            )
            solve.addStringValueInput("polar_distance_m", "Polar distance m", _setting_str(settings, "polar_distance_m"))
            solve.addStringValueInput("polar_angle_min_deg", "Polar angle min deg", _setting_str(settings, "polar_angle_min_deg"))
            solve.addStringValueInput("polar_angle_max_deg", "Polar angle max deg", _setting_str(settings, "polar_angle_max_deg"))
            solve.addStringValueInput("polar_angle_count", "Polar angle count", _setting_str(settings, "polar_angle_count"))
            solve.addBoolValueInput("mesh_only", "Mesh only (skip solves)", True, "", _setting_bool(settings, "mesh_only"))
            clamp_input = solve.addBoolValueInput(
                "clamp_to_mesh_limit",
                "Clamp solves to mesh-valid band",
                True,
                "",
                _setting_bool(settings, "clamp_to_mesh_limit"),
            )
            clamp_input.tooltip = (
                "When checked, each source solves only up to the conservative "
                "mesh-valid frequency (6 elements per wavelength). When "
                "unchecked, the full requested band is solved and results "
                "above the mesh-valid limit are increasingly inaccurate."
            )
            markers_input = solve.addBoolValueInput(
                "show_mesh_valid_markers",
                "Show mesh-valid markers on plots",
                True,
                "",
                _setting_bool(settings, "show_mesh_valid_markers"),
            )
            markers_input.tooltip = (
                "Overlay the mesh-valid (solid) and aperture-valid (dashed) "
                "frequency lines, and the shaded band between them, on the "
                "directivity heatmaps and response plots. Uncheck to hide the "
                "markers; the solve itself and its recorded mesh-valid limits "
                "are unaffected."
            )

            cardioid_group = inputs.addGroupCommandInput(
                "grp_passive_cardioid",
                "Passive cardioid MF",
            )
            cardioid_group.isExpanded = False
            cardioid = cardioid_group.children
            passive_enabled = cardioid.addBoolValueInput(
                "passive_cardioid_enabled",
                "Combine MF + port exit",
                True,
                "",
                _setting_bool(settings, "passive_cardioid_enabled"),
            )
            passive_enabled.tooltip = (
                "After direct solves, combine MF and PORT_EXIT through a "
                "rear-chamber plus resistive-port transfer."
            )
            cardioid.addStringValueInput(
                "passive_cardioid_rear_volume_l",
                "Rear chamber L",
                _setting_str(settings, "passive_cardioid_rear_volume_l"),
            )
            cardioid.addStringValueInput(
                "passive_cardioid_port_length_mm",
                "Port length mm",
                _setting_str(settings, "passive_cardioid_port_length_mm"),
            )
            port_area_input = cardioid.addStringValueInput(
                "passive_cardioid_port_area_cm2",
                "Port area cm2",
                _setting_str(settings, "passive_cardioid_port_area_cm2"),
            )
            port_area_input.tooltip = "Blank uses the tagged PORT_EXIT mesh area."
            cardioid.addStringValueInput(
                "passive_cardioid_foam_resistance_pa_s_m3",
                "Foam resistance Pa s/m3",
                _setting_str(settings, "passive_cardioid_foam_resistance_pa_s_m3"),
            )
            invert_input = cardioid.addBoolValueInput(
                "passive_cardioid_invert_port",
                "Rear-wave polarity",
                True,
                "",
                _setting_bool(settings, "passive_cardioid_invert_port"),
            )
            invert_input.tooltip = (
                "Default on: port is driven by the MF rear wave, opposite the "
                "front MF source polarity."
            )
            coupled_input = cardioid.addBoolValueInput(
                "passive_cardioid_coupled",
                "Couple driver LEM",
                True,
                "",
                _setting_bool(settings, "passive_cardioid_coupled"),
            )
            coupled_input.tooltip = (
                "Use the MF BEM radiation load with a voltage-driven "
                "Thiele/Small driver model."
            )
            driver_group = inputs.addGroupCommandInput(
                "grp_driver_lem",
                "Driver LEM (optional)",
            )
            driver_group.isExpanded = False
            driver_lem = driver_group.children
            for source_name, ts_id, volume_id in (
                ("LF", "lf_driver_lem", "lf_driver_rear_volume_l"),
                ("MF", "mf_driver_lem", "mf_driver_rear_volume_l"),
                ("HF", "hf_driver_lem", "hf_driver_rear_volume_l"),
            ):
                ts_input = driver_lem.addStringValueInput(
                    ts_id,
                    f"{source_name} driver T/S",
                    _setting_str(settings, ts_id),
                )
                ts_input.tooltip = (
                    "Paste Hornresp Key=Value text or enter a path to a Hornresp "
                    "driver file. Blank leaves this source uncoupled."
                )
                driver_lem.addStringValueInput(
                    volume_id,
                    f"{source_name} rear chamber L",
                    _setting_str(settings, volume_id),
                )
            driver_lem.addStringValueInput(
                "drive_voltage",
                "Drive voltage V RMS",
                _setting_str(settings, "drive_voltage"),
            )
            driver_lem.addStringValueInput(
                "rg_ohm",
                "Generator Rg ohm",
                _setting_str(settings, "rg_ohm"),
            )

            output_group = inputs.addGroupCommandInput("grp_output", "Output")
            output_group.isExpanded = True
            output = output_group.children
            output.addStringValueInput("output_root", "Output root", _setting_str(settings, "output_root"))
            output.addBoolValueInput("browse_output_root", "Browse output root", False, "", False)
            output.addBoolValueInput("open_output", "Open output folder", True, "", _setting_bool(settings, "open_output"))
            vituixcad_input = output.addBoolValueInput(
                "export_vituixcad",
                "Export VituixCAD FRDs",
                True,
                "",
                _setting_bool(settings, "export_vituixcad"),
            )
            vituixcad_input.tooltip = (
                "Write per-driver per-angle FRD sets (vituixcad/hor, "
                "vituixcad/ver) with a shared timing reference. When XO "
                "settings are filled, also write HornLab_active_lr4.vxp with "
                "the computed active LR4 filters, gains, and delays."
            )

            advanced_group = inputs.addGroupCommandInput("grp_advanced", "Advanced")
            advanced_group.isExpanded = False
            advanced = advanced_group.children
            mirror_plane = advanced.addDropDownCommandInput(
                "mirror_plane",
                "Mirror plane",
                adsk.core.DropDownStyles.TextListDropDownStyle,
            )
            selected_mirror_plane = _setting_str(settings, "mirror_plane")
            for name in (
                "Auto detect",
                "Left/Right + Front/Back",
                "Left/Right",
                "Front/Back",
                "Top/Bottom",
                "Full model",
            ):
                mirror_plane.listItems.add(name, selected_mirror_plane == name)
            advanced.addStringValueInput("python_path", "Python", _setting_str(settings, "python_path"))
            advanced.addBoolValueInput("browse_python_path", "Browse Python", False, "", False)
            advanced.addStringValueInput("wg_dir", "WG folder", _setting_str(settings, "wg_dir"))
            advanced.addBoolValueInput("browse_wg_dir", "Browse WG folder", False, "", False)
            advanced.addBoolValueInput("open_wg", "Launch WG", True, "", _setting_bool(settings, "open_wg"))

            input_changed_handler = CommandInputChangedHandler()
            command.inputChanged.add(input_changed_handler)
            _handlers.append(input_changed_handler)

            execute_handler = CommandExecuteHandler()
            command.execute.add(execute_handler)
            _handlers.append(execute_handler)

            _sync_passive_cardioid_ui(inputs)
            _update_size_prediction(inputs)
        except Exception:
            _show_message(traceback.format_exc())


# Inputs whose change re-runs the live size/cost estimate.
_PREDICTION_INPUT_IDS = frozenset(
    {
        "lf_mesh_mm",
        "mf_mesh_mm",
        "hf_mesh_mm",
        "port_exit_mesh_mm",
        "rigid_res_mm",
        "transition_mm",
        "freq_max_hz",
        "freq_count",
    }
)


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            changed = args.input
            input_id = changed.id if changed else ""
            inputs = args.inputs
            if input_id == "browse_output_root":
                _choose_folder(inputs, "output_root", "Select WG Metal output root")
                changed.value = False
            elif input_id == "browse_wg_dir":
                _choose_folder(inputs, "wg_dir", "Select Waveguide Generator folder")
                changed.value = False
            elif input_id == "browse_python_path":
                _choose_file(inputs, "python_path", "Select Python executable")
                changed.value = False
            elif input_id in {"passive_cardioid_enabled", "passive_cardioid_coupled"}:
                _sync_passive_cardioid_ui(inputs)
            elif input_id in _PREDICTION_INPUT_IDS:
                _update_size_prediction(inputs)
        except Exception:
            _show_message(traceback.format_exc())


def _choose_folder(inputs, target_input_id: str, title: str) -> None:
    ui = _ui()
    if ui is None:
        return
    target = _input_by_id(inputs, target_input_id)
    dialog = ui.createFolderDialog()
    dialog.title = title
    current = Path(str(target.value)).expanduser() if target else Path.home()
    if current.exists():
        dialog.initialDirectory = str(current if current.is_dir() else current.parent)
    result = dialog.showDialog()
    if result == adsk.core.DialogResults.DialogOK and target:
        target.value = dialog.folder


def _choose_file(inputs, target_input_id: str, title: str) -> None:
    ui = _ui()
    if ui is None:
        return
    target = _input_by_id(inputs, target_input_id)
    dialog = ui.createFileDialog()
    dialog.title = title
    current = Path(str(target.value)).expanduser() if target else Path.home()
    if current.exists():
        dialog.initialDirectory = str(current if current.is_dir() else current.parent)
    result = dialog.showOpen()
    if result == adsk.core.DialogResults.DialogOK and target:
        target.value = dialog.filename


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
        "PYTHONUSERBASE",
        "__PYVENV_LAUNCHER__",
    ):
        env.pop(key, None)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _background_popen_kwargs() -> dict:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _parse_required_positive_float(raw: str, label: str) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a positive number.") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise RuntimeError(f"{label} must be positive.")
    return value


def _parse_required_nonnegative_float(raw: str, label: str) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a non-negative number.") from exc
    if not math.isfinite(value) or value < 0.0:
        raise RuntimeError(f"{label} must be non-negative.")
    return value


def _parse_optional_nonnegative_float(raw: str, label: str) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be a number.") from exc
    if not math.isfinite(value) or value < 0.0:
        raise RuntimeError(f"{label} must be non-negative.")
    return value


def _launch_pipeline_background(
    cmd: list[str],
    out_dir: Path,
    step_path: Path,
    fusion_archive_path: Path,
) -> int:
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    started_at = _datetime.datetime.now().isoformat(timespec="seconds")
    launch_metadata_path = out_dir / "fusion_addin_launch.json"
    env = _subprocess_env()
    env["HORNLAB_FUSION_LAUNCH_METADATA"] = str(launch_metadata_path)

    stdout_path = logs_dir / "fusion_step_to_wg_pipeline.stdout.log"
    stderr_path = logs_dir / "fusion_step_to_wg_pipeline.stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            process = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=stdout,
                stderr=stderr,
                **_background_popen_kwargs(),
            )
    except Exception as exc:
        write_launch_metadata(
            launch_metadata_path,
            build_launch_metadata(
                command=cmd,
                pid=None,
                started_at=started_at,
                output_dir=out_dir,
                step_path=step_path,
                fusion_archive_path=fusion_archive_path,
                cwd=REPO_ROOT,
                status="launch_failed",
                error=str(exc),
            ),
        )
        raise

    write_launch_metadata(
        launch_metadata_path,
        build_launch_metadata(
            command=cmd,
            pid=int(process.pid),
            started_at=started_at,
            output_dir=out_dir,
            step_path=step_path,
            fusion_archive_path=fusion_archive_path,
            cwd=REPO_ROOT,
            status="running",
        ),
    )
    return int(process.pid)


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            app = adsk.core.Application.get()
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                raise RuntimeError("Active Fusion product is not a design.")

            inputs = args.command.commandInputs
            output_root = Path(str(_input_value(inputs, "output_root"))).expanduser()
            lf_mesh_mm = str(_input_value(inputs, "lf_mesh_mm") or "").strip()
            mf_mesh_mm = str(_input_value(inputs, "mf_mesh_mm") or "").strip()
            hf_mesh_mm = str(_input_value(inputs, "hf_mesh_mm") or "").strip()
            port_exit_mesh_mm = str(
                _input_value(inputs, "port_exit_mesh_mm") or ""
            ).strip()
            sources = build_source_specs(
                lf_mesh_mm=lf_mesh_mm,
                mf_mesh_mm=mf_mesh_mm,
                hf_mesh_mm=hf_mesh_mm,
                port_exit_mesh_mm=port_exit_mesh_mm,
            )
            freq_min_hz = str(_input_value(inputs, "freq_min_hz") or "50").strip()
            freq_max_hz = str(_input_value(inputs, "freq_max_hz") or "20000").strip()
            freq_count = str(_input_value(inputs, "freq_count") or "60").strip()
            freq_spacing = _selected_dropdown_name(inputs, "freq_spacing") or "log"
            crossover_lf_mf_hz = str(
                _input_value(inputs, "crossover_lf_mf_hz") or ""
            ).strip()
            crossover_mf_hf_hz = str(
                _input_value(inputs, "crossover_mf_hf_hz") or ""
            ).strip()
            polar_distance_m = str(_input_value(inputs, "polar_distance_m") or "2").strip()
            polar_angle_min_deg = str(_input_value(inputs, "polar_angle_min_deg") or "0").strip()
            polar_angle_max_deg = str(_input_value(inputs, "polar_angle_max_deg") or "180").strip()
            polar_angle_count = str(_input_value(inputs, "polar_angle_count") or "37").strip()
            transition_mm = str(_input_value(inputs, "transition_mm") or "200").strip()
            rigid_res_mm = str(_input_value(inputs, "rigid_res_mm") or "").strip()
            refine_raw = str(_input_value(inputs, "refine") or "").strip()
            refine = [entry.strip() for entry in refine_raw.split(",") if entry.strip()]
            mirror_plane = _selected_dropdown_name(inputs, "mirror_plane") or "Auto detect"
            symmetry_planes = symmetry_planes_for_mirror_plane(mirror_plane)
            mirror_axes = mirror_axes_for_symmetry_planes(symmetry_planes)
            quadrants = quadrants_for_symmetry_planes(symmetry_planes)
            python_path = Path(str(_input_value(inputs, "python_path"))).expanduser()
            wg_dir = Path(str(_input_value(inputs, "wg_dir"))).expanduser()
            mesh_only = bool(_input_value(inputs, "mesh_only"))
            clamp_to_mesh_limit = bool(_input_value(inputs, "clamp_to_mesh_limit"))
            show_mesh_valid_markers = bool(_input_value(inputs, "show_mesh_valid_markers"))
            export_vituixcad = bool(_input_value(inputs, "export_vituixcad"))
            passive_cardioid_enabled = bool(_input_value(inputs, "passive_cardioid_enabled"))
            passive_cardioid_rear_volume_l = str(
                _input_value(inputs, "passive_cardioid_rear_volume_l") or ""
            ).strip()
            passive_cardioid_port_length_mm = str(
                _input_value(inputs, "passive_cardioid_port_length_mm") or ""
            ).strip()
            passive_cardioid_port_area_cm2 = str(
                _input_value(inputs, "passive_cardioid_port_area_cm2") or ""
            ).strip()
            passive_cardioid_foam_resistance_pa_s_m3 = str(
                _input_value(inputs, "passive_cardioid_foam_resistance_pa_s_m3") or ""
            ).strip()
            passive_cardioid_invert_port = bool(
                _input_value(inputs, "passive_cardioid_invert_port")
            )
            passive_cardioid_coupled = bool(_input_value(inputs, "passive_cardioid_coupled"))
            lf_driver_lem = str(_input_value(inputs, "lf_driver_lem") or "").strip()
            mf_driver_lem = str(_input_value(inputs, "mf_driver_lem") or "").strip()
            hf_driver_lem = str(_input_value(inputs, "hf_driver_lem") or "").strip()
            lf_driver_rear_volume_l = str(
                _input_value(inputs, "lf_driver_rear_volume_l") or ""
            ).strip()
            mf_driver_rear_volume_l = str(
                _input_value(inputs, "mf_driver_rear_volume_l") or ""
            ).strip()
            hf_driver_rear_volume_l = str(
                _input_value(inputs, "hf_driver_rear_volume_l") or ""
            ).strip()
            drive_voltage = str(_input_value(inputs, "drive_voltage") or "").strip()
            rg_ohm = str(_input_value(inputs, "rg_ohm") or "").strip()
            underresolved_solve_policy = (
                "clamp-per-source" if clamp_to_mesh_limit else "warn"
            )
            open_wg = bool(_input_value(inputs, "open_wg"))
            open_output = bool(_input_value(inputs, "open_output"))

            if not sources:
                raise RuntimeError(
                    "At least one source mesh mm value is required."
                )
            if not PIPELINE_SCRIPT.exists():
                raise RuntimeError(f"Missing pipeline script: {PIPELINE_SCRIPT}")
            if not python_path.exists():
                raise RuntimeError(f"Python interpreter does not exist: {python_path}")
            if open_wg and not wg_dir.exists():
                raise RuntimeError(f"Waveguide Generator folder does not exist: {wg_dir}")
            if passive_cardioid_enabled:
                _parse_required_positive_float(
                    passive_cardioid_rear_volume_l,
                    "Passive cardioid rear chamber L",
                )
                _parse_required_nonnegative_float(
                    passive_cardioid_port_length_mm,
                    "Passive cardioid port length mm",
                )
                port_area = _parse_optional_nonnegative_float(
                    passive_cardioid_port_area_cm2,
                    "Passive cardioid port area cm2",
                )
                if port_area == 0.0:
                    raise RuntimeError("Passive cardioid port area cm2 must be positive or blank.")
                _parse_optional_nonnegative_float(
                    passive_cardioid_foam_resistance_pa_s_m3,
                    "Passive cardioid foam resistance",
                )
            if passive_cardioid_enabled and passive_cardioid_coupled and not mf_driver_lem:
                raise RuntimeError(
                    "Passive cardioid coupled mode requires MF driver T/S in Driver LEM."
                )
            for label, raw in (
                ("LF rear chamber L", lf_driver_rear_volume_l),
                ("MF rear chamber L", mf_driver_rear_volume_l),
                ("HF rear chamber L", hf_driver_rear_volume_l),
            ):
                if raw:
                    _parse_required_positive_float(raw, label)
            if drive_voltage:
                _parse_required_positive_float(drive_voltage, "Drive voltage V RMS")
            if rg_ohm:
                _parse_required_nonnegative_float(rg_ohm, "Generator Rg ohm")
            lf_mf_xo = (
                _parse_required_positive_float(crossover_lf_mf_hz, "LF/MF XO Hz")
                if crossover_lf_mf_hz
                else None
            )
            mf_hf_xo = (
                _parse_required_positive_float(crossover_mf_hf_hz, "MF/HF XO Hz")
                if crossover_mf_hf_hz
                else None
            )
            if lf_mf_xo is not None and mf_hf_xo is not None and lf_mf_xo >= mf_hf_xo:
                raise RuntimeError("LF/MF XO Hz must be below MF/HF XO Hz.")

            _save_settings({
                "settings_version": SETTINGS_VERSION,
                "output_root": str(output_root),
                "lf_mesh_mm": lf_mesh_mm,
                "mf_mesh_mm": mf_mesh_mm,
                "hf_mesh_mm": hf_mesh_mm,
                "port_exit_mesh_mm": port_exit_mesh_mm,
                "rigid_res_mm": rigid_res_mm,
                "refine": refine_raw,
                "freq_min_hz": freq_min_hz,
                "freq_max_hz": freq_max_hz,
                "freq_count": freq_count,
                "freq_spacing": freq_spacing,
                "crossover_lf_mf_hz": crossover_lf_mf_hz,
                "crossover_mf_hf_hz": crossover_mf_hf_hz,
                "polar_distance_m": polar_distance_m,
                "polar_angle_min_deg": polar_angle_min_deg,
                "polar_angle_max_deg": polar_angle_max_deg,
                "polar_angle_count": polar_angle_count,
                "transition_mm": transition_mm,
                "mirror_plane": mirror_plane,
                "python_path": str(python_path),
                "wg_dir": str(wg_dir),
                "mesh_only": mesh_only,
                "open_wg": open_wg,
                "open_output": open_output,
                "export_vituixcad": export_vituixcad,
                "clamp_to_mesh_limit": clamp_to_mesh_limit,
                "show_mesh_valid_markers": show_mesh_valid_markers,
                "passive_cardioid_enabled": passive_cardioid_enabled,
                "passive_cardioid_rear_volume_l": passive_cardioid_rear_volume_l,
                "passive_cardioid_port_length_mm": passive_cardioid_port_length_mm,
                "passive_cardioid_port_area_cm2": passive_cardioid_port_area_cm2,
                "passive_cardioid_foam_resistance_pa_s_m3": (
                    passive_cardioid_foam_resistance_pa_s_m3
                ),
                "passive_cardioid_invert_port": passive_cardioid_invert_port,
                "passive_cardioid_coupled": passive_cardioid_coupled,
                "lf_driver_lem": lf_driver_lem,
                "mf_driver_lem": mf_driver_lem,
                "hf_driver_lem": hf_driver_lem,
                "lf_driver_rear_volume_l": lf_driver_rear_volume_l,
                "mf_driver_rear_volume_l": mf_driver_rear_volume_l,
                "hf_driver_rear_volume_l": hf_driver_rear_volume_l,
                "drive_voltage": drive_voltage,
                "rg_ohm": rg_ohm,
            })

            stamp = _datetime.datetime.now().strftime("%y%m%d-%H%M%S")
            design_name = _safe_name(_active_design_name(design))
            out_dir = output_root / f"{stamp}-{design_name}"
            out_dir.mkdir(parents=True, exist_ok=True)
            step_path = out_dir / f"{design_name}.step"
            fusion_archive_path = out_dir / f"{design_name}.f3d"

            _export_step(design, step_path)
            _export_fusion_archive(design, fusion_archive_path)

            cmd = build_pipeline_command(
                python_path=python_path,
                pipeline_script=PIPELINE_SCRIPT,
                step_path=step_path,
                out_dir=out_dir,
                sources=sources,
                transition_mm=transition_mm,
                rigid_res_mm=rigid_res_mm,
                refine=refine,
                quadrants=quadrants,
                mirror_axes=mirror_axes,
                symmetry_planes=symmetry_planes,
                freq_min_hz=freq_min_hz,
                freq_max_hz=freq_max_hz,
                freq_count=freq_count,
                freq_spacing=freq_spacing,
                crossover_lf_mf_hz=crossover_lf_mf_hz,
                crossover_mf_hf_hz=crossover_mf_hf_hz,
                polar_distance_m=polar_distance_m,
                polar_angle_min_deg=polar_angle_min_deg,
                polar_angle_max_deg=polar_angle_max_deg,
                polar_angle_count=polar_angle_count,
                wg_dir=wg_dir,
                mesh_only=mesh_only,
                open_wg=open_wg,
                open_output=open_output,
                underresolved_solve_policy=underresolved_solve_policy,
                show_mesh_valid_markers=show_mesh_valid_markers,
                export_vituixcad=export_vituixcad,
                passive_cardioid_enabled=passive_cardioid_enabled,
                passive_cardioid_rear_volume_l=passive_cardioid_rear_volume_l,
                passive_cardioid_port_length_mm=passive_cardioid_port_length_mm,
                passive_cardioid_port_area_cm2=passive_cardioid_port_area_cm2,
                passive_cardioid_foam_resistance_pa_s_m3=(
                    passive_cardioid_foam_resistance_pa_s_m3
                ),
                passive_cardioid_invert_port=passive_cardioid_invert_port,
                passive_cardioid_coupled=passive_cardioid_coupled,
                lf_driver_lem=lf_driver_lem,
                mf_driver_lem=mf_driver_lem,
                hf_driver_lem=hf_driver_lem,
                lf_driver_rear_volume_l=lf_driver_rear_volume_l,
                mf_driver_rear_volume_l=mf_driver_rear_volume_l,
                hf_driver_rear_volume_l=hf_driver_rear_volume_l,
                drive_voltage=drive_voltage,
                rg_ohm=rg_ohm,
            )
            pid = _launch_pipeline_background(
                cmd,
                out_dir,
                step_path,
                fusion_archive_path,
            )
            paths = expected_pipeline_paths(out_dir)

            message = (
                "WG Metal pipeline started in the background.\n\n"
                f"Output:\n{out_dir}\n\n"
                f"Fusion archive:\n{fusion_archive_path}\n\n"
                f"PID: {pid}\n\n"
                "Sources are matched automatically against the design; missing "
                "ones are skipped. Symmetry planes are auto-detected unless "
                "overridden under Advanced. A notification appears when the "
                "pipeline finishes.\n\n"
                f"Launch/status:\n{out_dir / 'fusion_addin_launch.json'}\n\n"
                "Pipeline logs:\n"
                f"{paths['launcher_stdout']}\n"
                f"{paths['launcher_stderr']}\n\n"
                "Fusion can be used while the pipeline runs."
            )
            if not mesh_only:
                clamp_estimate = estimate_clamped_solve_band(
                    sources=sources,
                    rigid_res_mm=rigid_res_mm,
                    freq_max_hz=freq_max_hz,
                )
                if clamp_estimate:
                    bands = ", ".join(
                        f"{name} to about {hz:.0f} Hz"
                        for name, hz in sorted(clamp_estimate.items())
                    )
                    if clamp_to_mesh_limit:
                        note = (
                            f"NOTE: the requested solve maximum of {freq_max_hz} Hz "
                            "exceeds what the manual-mm mesh resolves. The solve clamps "
                            f"{bands} (conservative limit at "
                            "6 elements per wavelength; the exact limits come from "
                            "the prepared mesh). Refine 'Rigid body mesh mm' and "
                            "the source mesh mm values to solve higher, or untick "
                            "'Clamp solves to mesh-valid band' to solve the full "
                            "band anyway."
                        )
                    else:
                        note = (
                            f"NOTE: the full band up to {freq_max_hz} Hz is solved, "
                            f"but the manual-mm mesh only resolves {bands} (conservative "
                            "limit at 6 elements per wavelength). Results above "
                            "those limits are increasingly inaccurate. Refine "
                            "'Rigid body mesh mm' and the source mesh mm values "
                            "to push the trustworthy band higher."
                        )
                    message = f"{note}\n\n{message}"
            stale_warning = _stale_install_warning()
            if stale_warning:
                message = f"{stale_warning}\n\n{message}"
            _show_message(message)
        except Exception:
            _show_message(traceback.format_exc())


def _find_addins_panel(ui):
    workspace = ui.workspaces.itemById("FusionSolidEnvironment")
    panel = workspace.toolbarPanels.itemById("SolidScriptsAddinsPanel") if workspace else None
    if panel is None:
        panel = ui.allToolbarPanels.itemById("SolidScriptsAddinsPanel")
    return panel


def _delete_quietly(obj) -> None:
    """Delete a Fusion API object, tolerating already-deleted references.

    A duplicate registration of this add-in (e.g. the AddIns folder install
    plus a manually added repo path) deletes the other instance's command
    definition from under it; deleteMe then raises on the stale reference.
    """
    try:
        if obj and obj.isValid:
            obj.deleteMe()
    except Exception:
        pass


def run(context):
    global _control, _command_definition
    try:
        ui = _ui()
        if ui is None:
            return

        existing = ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()

        _command_definition = ui.commandDefinitions.addButtonDefinition(
            CMD_ID,
            CMD_NAME,
            CMD_DESCRIPTION,
        )
        created_handler = CommandCreatedHandler()
        _command_definition.commandCreated.add(created_handler)
        _handlers.append(created_handler)

        panel = _find_addins_panel(ui)
        if panel is None:
            raise RuntimeError("Could not find Fusion Scripts and Add-Ins toolbar panel.")

        stale_control = panel.controls.itemById(CMD_ID)
        if stale_control:
            _delete_quietly(stale_control)

        _control = panel.controls.addCommand(_command_definition)
    except Exception:
        _show_message(traceback.format_exc())


def stop(context):
    global _control, _command_definition
    try:
        _delete_quietly(_control)
        _control = None
        _delete_quietly(_command_definition)
        _command_definition = None

        ui = _ui()
        if ui:
            panel = _find_addins_panel(ui)
            if panel:
                _delete_quietly(panel.controls.itemById(CMD_ID))
            _delete_quietly(ui.commandDefinitions.itemById(CMD_ID))
    except Exception:
        _show_message(traceback.format_exc())
