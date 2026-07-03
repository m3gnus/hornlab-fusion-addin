from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGMetalPipeline" / "WGMetalPipeline.py"


class _EventHandler:
    pass


def _load_addin_with_fake_adsk():
    core = types.SimpleNamespace(
        CommandCreatedEventHandler=_EventHandler,
        InputChangedEventHandler=_EventHandler,
        CommandEventHandler=_EventHandler,
        Application=types.SimpleNamespace(get=lambda: None),
        DropDownStyles=types.SimpleNamespace(TextListDropDownStyle=1),
        DialogResults=types.SimpleNamespace(DialogOK=1),
    )
    fusion = types.SimpleNamespace(
        Design=types.SimpleNamespace(cast=lambda value: value),
    )
    adsk = types.SimpleNamespace(core=core, fusion=fusion)
    sys.modules["adsk"] = adsk
    sys.modules["adsk.core"] = core
    sys.modules["adsk.fusion"] = fusion

    spec = importlib.util.spec_from_file_location("WGMetalPipeline_test", ADDIN)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Input:
    def __init__(self, input_id: str, value=None, selected=None, children=None):
        self.id = input_id
        self.value = value
        self.selectedItem = selected
        self.children = children
        self.isEnabled = True


class _Selected:
    def __init__(self, name: str):
        self.name = name


class _Inputs:
    def __init__(self, items):
        self._items = list(items)
        self.count = len(self._items)

    def itemById(self, input_id: str):
        for item in self._items:
            if item.id == input_id:
                return item
        return None

    def item(self, index: int):
        return self._items[index]


def test_addin_input_lookup_finds_nested_group_children():
    addin = _load_addin_with_fake_adsk()
    nested = _Inputs(
        [
            _Input("python_path", "/venv/bin/python"),
            _Input("mirror_plane", selected=_Selected("Auto detect")),
        ]
    )
    top = _Inputs([_Input("grp_advanced", children=nested)])

    assert addin._input_value(top, "python_path") == "/venv/bin/python"
    assert addin._selected_dropdown_name(top, "mirror_plane") == "Auto detect"


def test_fusion_archive_export_uses_native_archive_options(tmp_path):
    addin = _load_addin_with_fake_adsk()
    archive_path = tmp_path / "design.f3d"

    class _ExportManager:
        def __init__(self):
            self.archive_options_path = None
            self.executed_options = None

        def createFusionArchiveExportOptions(self, path):
            self.archive_options_path = path
            return {"kind": "fusion_archive", "path": path}

        def execute(self, options):
            self.executed_options = options
            return True

    manager = _ExportManager()
    design = types.SimpleNamespace(exportManager=manager)

    addin._export_fusion_archive(design, archive_path)

    assert manager.archive_options_path == str(archive_path)
    assert manager.executed_options == {
        "kind": "fusion_archive",
        "path": str(archive_path),
    }


def test_passive_cardioid_sync_preserves_requested_polar_window():
    addin = _load_addin_with_fake_adsk()
    top = _Inputs(
        [
            _Input("passive_cardioid_enabled", True),
            _Input("passive_cardioid_rear_volume_l", "10"),
            _Input("passive_cardioid_port_length_mm", "25"),
            _Input("passive_cardioid_port_area_cm2", ""),
            _Input("passive_cardioid_foam_resistance_pa_s_m3", "40000"),
            _Input("passive_cardioid_invert_port", True),
            _Input("polar_angle_min_deg", "-90"),
            _Input("polar_angle_max_deg", "90"),
            _Input("polar_angle_count", "100"),
        ]
    )

    addin._sync_passive_cardioid_ui(top)

    assert addin._input_value(top, "polar_angle_min_deg") == "-90"
    assert addin._input_value(top, "polar_angle_max_deg") == "90"
    assert addin._input_value(top, "polar_angle_count") == "100"
    assert addin._input_by_id(top, "passive_cardioid_rear_volume_l").isEnabled is True


def test_driver_lem_defaults_present():
    addin = _load_addin_with_fake_adsk()

    assert addin.SETTINGS_VERSION == 13
    assert addin.DEFAULT_SETTINGS["passive_cardioid_coupled"] is False
    assert addin.DEFAULT_SETTINGS["lf_driver_lem"] == ""
    assert addin.DEFAULT_SETTINGS["mf_driver_lem"] == ""
    assert addin.DEFAULT_SETTINGS["hf_driver_lem"] == ""
    assert addin.DEFAULT_SETTINGS["lf_driver_rear_volume_l"] == ""
    assert addin.DEFAULT_SETTINGS["mf_driver_rear_volume_l"] == ""
    assert addin.DEFAULT_SETTINGS["hf_driver_rear_volume_l"] == ""
    assert addin.DEFAULT_SETTINGS["drive_voltage"] == "2.83"
    assert addin.DEFAULT_SETTINGS["rg_ohm"] == "0"
    assert addin.DEFAULT_SETTINGS["open_report"] is False
    assert addin.DEFAULT_SETTINGS["output_per_driver_plots"] is True
    assert addin.DEFAULT_SETTINGS["output_combined_set"] is True
    assert addin.DEFAULT_SETTINGS["output_passive_cardioid_set"] is True
    assert addin.DEFAULT_SETTINGS["output_driver_lem_artifacts"] is True
    assert addin.DEFAULT_SETTINGS["output_derived_acoustics"] is True
    assert addin.DEFAULT_SETTINGS["export_vituixcad"] is False
    assert addin.DEFAULT_SETTINGS["output_radiation_impedance"] is True
    assert addin.DEFAULT_SETTINGS["output_pressure_bases"] is True
    assert addin.DEFAULT_SETTINGS["output_run_report"] is True
    assert "passive_cardioid_driver_sd_cm2" not in addin.DEFAULT_SETTINGS
    assert "passive_cardioid_drive_voltage" not in addin.DEFAULT_SETTINGS


def test_passive_cardioid_sync_leaves_driver_lem_fields_enabled():
    addin = _load_addin_with_fake_adsk()
    driver_ids = [
        "lf_driver_lem",
        "mf_driver_lem",
        "hf_driver_lem",
        "lf_driver_rear_volume_l",
        "mf_driver_rear_volume_l",
        "hf_driver_rear_volume_l",
        "drive_voltage",
        "rg_ohm",
    ]
    top = _Inputs(
        [
            _Input("passive_cardioid_enabled", False),
            _Input("passive_cardioid_rear_volume_l", "10"),
            _Input("passive_cardioid_port_length_mm", "25"),
            _Input("passive_cardioid_port_area_cm2", ""),
            _Input("passive_cardioid_foam_resistance_pa_s_m3", "40000"),
            _Input("passive_cardioid_invert_port", True),
            _Input("passive_cardioid_coupled", True),
            *[_Input(input_id, "1") for input_id in driver_ids],
        ]
    )

    addin._sync_passive_cardioid_ui(top)

    assert addin._input_by_id(top, "passive_cardioid_coupled").isEnabled is False
    for input_id in driver_ids:
        assert addin._input_by_id(top, input_id).isEnabled is True

    addin._input_by_id(top, "passive_cardioid_enabled").value = True
    addin._input_by_id(top, "passive_cardioid_coupled").value = False
    addin._sync_passive_cardioid_ui(top)

    assert addin._input_by_id(top, "passive_cardioid_coupled").isEnabled is True
    for input_id in driver_ids:
        assert addin._input_by_id(top, input_id).isEnabled is True

    addin._input_by_id(top, "passive_cardioid_coupled").value = True
    addin._sync_passive_cardioid_ui(top)

    for input_id in driver_ids:
        assert addin._input_by_id(top, input_id).isEnabled is True


def test_settings_migration_scopes_stale_keys_per_version(tmp_path, monkeypatch):
    addin = _load_addin_with_fake_adsk()
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(addin, "SETTINGS_PATH", settings_path)

    # A v10 file keeps its live choices; removed mesh sizing controls are
    # dropped at v11.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": 10,
                "mesh_sizing_mode": "frequency-role",
                "radiating_epw": "6",
                "shadow_epw": "2.5",
                "throat_epw": "8",
                "mirror_plane": "Front/Back",
                "clamp_to_mesh_limit": True,
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    assert "mesh_sizing_mode" not in settings
    assert "radiating_epw" not in settings
    assert "shadow_epw" not in settings
    assert "throat_epw" not in settings
    assert settings["mirror_plane"] == "Front/Back"
    assert settings["clamp_to_mesh_limit"] is True

    # A v11 file drops the removed passive-cardioid driver controls at v12.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": 11,
                "passive_cardioid_driver_sd_cm2": "320",
                "passive_cardioid_driver_bl_tm": "11.6",
                "passive_cardioid_driver_re_ohm": "5.2",
                "passive_cardioid_driver_le_mh": "0.8",
                "passive_cardioid_driver_le2_mh": "0.2",
                "passive_cardioid_driver_re2_ohm": "3.5",
                "passive_cardioid_driver_mmd_g": "26.2",
                "passive_cardioid_driver_mms_g": "29.4",
                "passive_cardioid_driver_cms_mm_per_n": "0.252",
                "passive_cardioid_driver_vas_l": "33",
                "passive_cardioid_driver_fs_hz": "55",
                "passive_cardioid_driver_rms_kg_per_s": "3.18",
                "passive_cardioid_driver_qms": "4.1",
                "passive_cardioid_driver_count": "2",
                "passive_cardioid_drive_voltage": "4",
                "passive_cardioid_rg_ohm": "0.2",
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    for stale_key in (
        "passive_cardioid_driver_sd_cm2",
        "passive_cardioid_driver_bl_tm",
        "passive_cardioid_driver_re_ohm",
        "passive_cardioid_driver_le_mh",
        "passive_cardioid_driver_le2_mh",
        "passive_cardioid_driver_re2_ohm",
        "passive_cardioid_driver_mmd_g",
        "passive_cardioid_driver_mms_g",
        "passive_cardioid_driver_cms_mm_per_n",
        "passive_cardioid_driver_vas_l",
        "passive_cardioid_driver_fs_hz",
        "passive_cardioid_driver_rms_kg_per_s",
        "passive_cardioid_driver_qms",
        "passive_cardioid_driver_count",
        "passive_cardioid_drive_voltage",
        "passive_cardioid_rg_ohm",
    ):
        assert stale_key not in settings
    assert settings["mf_driver_lem"] == ""
    assert settings["drive_voltage"] == "2.83"
    assert settings["rg_ohm"] == "0"

    # A coupled v11 file migrates the removed passive-cardioid driver controls
    # into the current MF driver T/S field before the stale keys are dropped.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": 11,
                "passive_cardioid_coupled": True,
                "passive_cardioid_driver_sd_cm2": "320",
                "passive_cardioid_driver_bl_tm": "11.6",
                "passive_cardioid_driver_re_ohm": "5.2",
                "passive_cardioid_driver_le_mh": "0.8",
                "passive_cardioid_driver_le2_mh": "0.2",
                "passive_cardioid_driver_re2_ohm": "3.5",
                "passive_cardioid_driver_mmd_g": "26.2",
                "passive_cardioid_driver_mms_g": "29.4",
                "passive_cardioid_driver_cms_mm_per_n": "0.252",
                "passive_cardioid_driver_vas_l": "33",
                "passive_cardioid_driver_fs_hz": "55",
                "passive_cardioid_driver_rms_kg_per_s": "3.18",
                "passive_cardioid_driver_qms": "4.1",
                "passive_cardioid_driver_count": "2",
                "passive_cardioid_drive_voltage": "4",
                "passive_cardioid_rg_ohm": "0.2",
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    assert settings["passive_cardioid_coupled"] is True
    assert settings["drive_voltage"] == "4"
    assert settings["rg_ohm"] == "0.2"
    spec = addin._fusion_pipeline_launch.parse_driver_lem_spec(
        "MF",
        settings["mf_driver_lem"],
    )
    assert spec.params["sd_m2"] == pytest.approx(0.032)
    assert spec.params["cms_m_per_n"] == pytest.approx(0.000252)
    assert spec.params["n_drivers"] == 2
    for stale_key in (
        "passive_cardioid_driver_sd_cm2",
        "passive_cardioid_driver_bl_tm",
        "passive_cardioid_driver_re_ohm",
        "passive_cardioid_driver_le_mh",
        "passive_cardioid_driver_le2_mh",
        "passive_cardioid_driver_re2_ohm",
        "passive_cardioid_driver_mmd_g",
        "passive_cardioid_driver_mms_g",
        "passive_cardioid_driver_cms_mm_per_n",
        "passive_cardioid_driver_vas_l",
        "passive_cardioid_driver_fs_hz",
        "passive_cardioid_driver_rms_kg_per_s",
        "passive_cardioid_driver_qms",
        "passive_cardioid_driver_count",
        "passive_cardioid_drive_voltage",
        "passive_cardioid_rg_ohm",
    ):
        assert stale_key not in settings

    # An incomplete coupled v11 driver cannot launch safely, so migration
    # disables coupled mode instead of keeping a runnable-looking bad config.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": 11,
                "passive_cardioid_coupled": True,
                "passive_cardioid_driver_sd_cm2": "320",
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    assert settings["passive_cardioid_coupled"] is False
    assert settings["mf_driver_lem"] == ""

    # A pre-v9 file drops the whole stale set.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": 8,
                "mesh_sizing_mode": "frequency-role",
                "mirror_plane": "Front/Back",
                "clamp_to_mesh_limit": True,
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    assert "mesh_sizing_mode" not in settings
    assert settings["mirror_plane"] == "Auto detect"
    assert settings["clamp_to_mesh_limit"] is False

    # A current-version file keeps live settings.
    settings_path.write_text(
        json.dumps(
            {
                "settings_version": addin.SETTINGS_VERSION,
                "mirror_plane": "Front/Back",
                "export_vituixcad": True,
            }
        ),
        encoding="utf-8",
    )
    settings = addin._load_settings()
    assert settings["mirror_plane"] == "Front/Back"
    assert settings["export_vituixcad"] is True


def test_parse_helpers_reject_non_finite_values():
    addin = _load_addin_with_fake_adsk()
    for raw in ("nan", "inf", "-inf"):
        with pytest.raises(RuntimeError):
            addin._parse_required_positive_float(raw, "XO Hz")
        with pytest.raises(RuntimeError):
            addin._parse_required_nonnegative_float(raw, "Port length")
        with pytest.raises(RuntimeError):
            addin._parse_optional_nonnegative_float(raw, "Port area")
