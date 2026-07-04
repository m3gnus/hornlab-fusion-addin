from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "fusion-addins" / "WGMetalPipeline" / "fusion_pipeline_launch.py"
PIPELINE = ROOT / "scripts" / "fusion_step_to_wg_pipeline.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("fusion_pipeline_launch", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_pipeline():
    spec = importlib.util.spec_from_file_location("fusion_step_to_wg_pipeline", PIPELINE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_manifest_path(run_dir: Path, name: str) -> Path:
    path = run_dir / "manifests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _helper_command(helper, **overrides):
    kwargs = dict(
        python_path=Path("/venv/bin/python"),
        pipeline_script=Path("/repo/scripts/fusion_step_to_wg_pipeline.py"),
        step_path=Path("/out/design.step"),
        out_dir=Path("/out/run"),
        sources="LF:20,MF:10,HF:5",
        transition_mm="200",
        rigid_res_mm="20",
        freq_min_hz="50",
        freq_max_hz="20000",
        freq_count="60",
        freq_spacing="log",
        polar_distance_m="2",
        polar_angle_min_deg="0",
        polar_angle_max_deg="180",
        polar_angle_count="37",
        wg_dir=Path("/wg"),
        mesh_only=False,
        open_wg=False,
        open_output=True,
    )
    kwargs.update(overrides)
    return helper.build_pipeline_command(**kwargs)


def _coupled_helper_overrides():
    return dict(
        passive_cardioid_enabled=True,
        passive_cardioid_rear_volume_l="4",
        passive_cardioid_port_length_mm="30",
        passive_cardioid_port_area_cm2="120",
        passive_cardioid_foam_resistance_pa_s_m3="2000",
        passive_cardioid_invert_port=True,
        passive_cardioid_coupled=True,
        mf_driver_lem=(
            "Sd=320,Bl=11.6,Re=5.2,Le=0.8,Mms=29.4,"
            "Cms=0.000252,Qms=4.1"
        ),
        drive_voltage="2.83",
        rg_ohm="0.1",
    )


def test_pipeline_parses_and_forwards_every_coupled_flag_the_addin_emits():
    """Dialog-launched coupled runs must not die at the pipeline argparse,
    and every coupled option the dialog emits must be in the forward table."""
    helper = _load_helper()
    pipeline = _load_pipeline()

    cmd = _helper_command(helper, **_coupled_helper_overrides())
    args = pipeline.parse_args(cmd[2:])

    assert args.plot_theme == "hornlab"
    assert args.passive_cardioid_coupled is True
    assert args.driver_lem == [
        "MF:Sd=320,Bl=11.6,Re=5.2,Le=0.8,Mms=29.4,Cms=0.000252,Qms=4.1,N=1"
    ]
    assert args.drive_voltage == pytest.approx(2.83)
    assert args.rg_ohm == pytest.approx(0.1)
    assert args.passive_cardioid_drive_voltage is None
    assert args.passive_cardioid_rg_ohm is None

    forwarded = {
        option
        for option, _attr in pipeline.PASSIVE_CARDIOID_COUPLED_FORWARD_OPTIONS
    }
    emitted = {
        token
        for token in cmd
        if token.startswith("--passive-cardioid-driver-")
        or token in ("--passive-cardioid-drive-voltage", "--passive-cardioid-rg-ohm")
    }
    assert emitted <= forwarded
    emitted_driver_lem = {
        token
        for token in cmd
        if token in ("--driver-lem", "--driver-rear-volume-l", "--drive-voltage", "--rg-ohm")
    }
    driver_lem_forwarded = {
        option for option, _attr in pipeline.DRIVER_LEM_REPEATABLE_FORWARD_OPTIONS
    } | {option for option, _attr in pipeline.DRIVER_LEM_VALUE_FORWARD_OPTIONS}
    assert emitted_driver_lem <= driver_lem_forwarded


def test_driver_lem_parser_accepts_hornresp_file_and_normalizes_units(tmp_path):
    helper = _load_helper()
    driver_file = tmp_path / "BC 10CL51.txt"
    driver_file.write_text(
        "\n".join(
            [
                "BC 10CL51",
                "Sd=320.0",
                "Bl=11.6",
                "Cms=252.0E-06",
                "Rms=3.18",
                "Mmd=26.2",
                "Le=0.8",
                "Re=5.2",
                "Xmax=5.5",
                "Leb=0.00",
                "Ke=0.00",
                "Rss=0.00",
                "Le=0.00",
                "Rms=0.00",
            ]
        ),
        encoding="utf-8",
    )

    spec = helper.parse_driver_lem_spec("MF", str(driver_file))

    assert spec.name == "MF"
    assert spec.params["sd_m2"] == pytest.approx(0.032)
    assert spec.params["cms_m_per_n"] == pytest.approx(252.0e-6)
    assert spec.params["mmd_kg"] == pytest.approx(0.0262)
    assert spec.params["le_h"] == pytest.approx(0.0008)
    assert spec.params["rms_kg_per_s"] == pytest.approx(3.18)
    assert spec.params["xmax_m"] == pytest.approx(0.0055)
    assert spec.params["n_drivers"] == 1
    assert any("Leb" in warning for warning in spec.warnings)
    assert any("duplicate Le=0.00 ignored" in warning for warning in spec.warnings)


def test_driver_lem_parser_accepts_mms_and_lr2_pair():
    helper = _load_helper()

    spec = helper.parse_driver_lem_cli_entry(
        "LF:Sd=500,Bl=15,Re=6.1,Mms=55,Cms=3.5e-4,Rms=4.2,Le=1.2,Le2=0.3,Re2=4,N=2"
    )

    assert spec.params["mms_kg"] == pytest.approx(0.055)
    assert spec.params["le2_h"] == pytest.approx(0.0003)
    assert spec.params["re2_ohm"] == pytest.approx(4.0)
    assert spec.params["n_drivers"] == 2
    assert "Mms=55" in spec.canonical_payload()


def test_driver_database_loader_builds_lem_payloads_from_csv(tmp_path):
    helper = _load_helper()
    database = tmp_path / "drivers.csv"
    database.write_text(
        "\n".join(
            [
                "Brand,Model,Size_in,Z_ohm,Fs_Hz,Qms,Vas_L,Sd_cm2,Bl_Tm,Re_ohm,Le_mH,Mms_g,Cms_mm_per_N,Xmax_mm",
                "B&C,10CL51,10,8,58,3.5,36,320,11.6,5.2,0.8,30.5,0.252,5.5",
                "Broken,NoMotor,10,8,58,3.5,36,320,,5.2,0.8,30.5,0.252,5.5",
                "Big,LFOnly,15,8,38,5.1,100,855,22,5.4,1.2,100,,7",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    mf_entries = helper.load_driver_database_entries(
        source_name="MF",
        database_paths=[database],
    )

    assert [entry.label for entry in mf_entries] == [
        "B&C 10CL51 (10 in, 8 ohm) - drivers"
    ]
    spec = helper.parse_driver_lem_spec("MF", mf_entries[0].payload)
    assert spec.params["sd_m2"] == pytest.approx(0.032)
    assert spec.params["cms_m_per_n"] == pytest.approx(0.000252)
    assert spec.params["mms_kg"] == pytest.approx(0.0305)
    assert spec.params["xmax_m"] == pytest.approx(0.0055)

    lf_payloads = helper.driver_database_payloads_by_label(
        source_name="LF",
        database_paths=[database],
    )
    assert "Big LFOnly (15 in, 8 ohm) - drivers" in lf_payloads
    assert "Vas=100" in lf_payloads["Big LFOnly (15 in, 8 ohm) - drivers"]


def test_pipeline_parser_accepts_and_groups_new_driver_lem_flags():
    pipeline = _load_pipeline()
    args = pipeline.parse_args(
        [
            "--step", "/out/design.step",
            "--out", "/out/run",
            "--sources", "LF:20,MF:10",
            "--driver-lem", "LF:Sd=320,Bl=11.6,Re=5.2,Mmd=26.2,Cms=2.52e-4,Rms=3.18",
            "--driver-rear-volume-l", "LF:4.5",
            "--drive-voltage", "4.0",
            "--rg-ohm", "0.2",
        ]
    )

    assert args.driver_lem == [
        "LF:Sd=320,Bl=11.6,Re=5.2,Mmd=26.2,Cms=2.52e-4,Rms=3.18"
    ]
    assert args.driver_rear_volume_l == ["LF:4.5"]
    assert args.drive_voltage == pytest.approx(4.0)
    assert args.rg_ohm == pytest.approx(0.2)
    assert {
        option for option, _ in pipeline.DRIVER_LEM_REPEATABLE_FORWARD_OPTIONS
    } == {"--driver-lem", "--driver-rear-volume-l"}
    assert {
        option for option, _ in pipeline.DRIVER_LEM_VALUE_FORWARD_OPTIONS
    } == {"--drive-voltage", "--rg-ohm"}


def test_estimate_clamped_solve_band_flags_shadow_limited_sources():
    helper = _load_helper()

    clamped = helper.estimate_clamped_solve_band(
        sources="LF:30,MF:15,HF:5",
        rigid_res_mm="30",
        freq_max_hz="20000",
    )

    assert clamped is not None
    assert set(clamped) == {"LF", "MF", "HF"}
    expected = 343000.0 / (6.0 * 30.0)
    for ceiling_hz in clamped.values():
        assert abs(ceiling_hz - expected) < 1.0


def test_estimate_clamped_solve_band_tracks_manual_background_cap():
    helper = _load_helper()

    clamped = helper.estimate_clamped_solve_band(
        sources="LF:20,HF:5",
        rigid_res_mm="30",
        freq_max_hz="10000",
    )
    assert clamped is not None
    assert clamped["HF"] == pytest.approx(343000.0 / (6.0 * 30.0), rel=1e-3)


def test_estimate_clamped_solve_band_none_when_request_fits():
    helper = _load_helper()

    clamped = helper.estimate_clamped_solve_band(
        sources="HF:5",
        rigid_res_mm="5",
        freq_max_hz="2000",
    )

    assert clamped is None


def test_estimate_clamped_solve_band_defaults_rigid_to_coarsest_source():
    helper = _load_helper()

    clamped = helper.estimate_clamped_solve_band(
        sources="LF:20,HF:5",
        rigid_res_mm="",
        freq_max_hz="20000",
    )

    assert clamped is not None
    expected = 343000.0 / (6.0 * 20.0)
    for ceiling_hz in clamped.values():
        assert abs(ceiling_hz - expected) < 1.0


def test_estimate_clamped_solve_band_tolerates_unparseable_input():
    helper = _load_helper()

    assert helper.estimate_clamped_solve_band(
        sources="LF:abc",
        rigid_res_mm="20",
        freq_max_hz="20000",
    ) is None
    assert helper.estimate_clamped_solve_band(
        sources="",
        rigid_res_mm="20",
        freq_max_hz="20000",
    ) is None


def test_build_pipeline_command_defaults_to_automatic_pipeline():
    helper = _load_helper()

    cmd = _helper_command(helper)

    assert cmd[:6] == [
        "/venv/bin/python",
        "/repo/scripts/fusion_step_to_wg_pipeline.py",
        "--step",
        "/out/design.step",
        "--out",
        "/out/run",
    ]
    assert cmd[cmd.index("--sources") + 1] == "LF:20,MF:10,HF:5"
    assert "--mesh-sizing-mode" not in cmd
    assert cmd[cmd.index("--symmetry-planes") + 1] == "auto"
    assert "--quadrants" not in cmd
    assert "--mirror-axes" not in cmd
    assert cmd[cmd.index("--polar-angle-count") + 1] == "37"
    assert cmd[cmd.index("--plot-theme") + 1] == "hornlab"
    assert cmd[cmd.index("--bem-formulation") + 1] == "complex_k"
    assert cmd[cmd.index("--complex-k-shift") + 1] == "0.005"
    assert cmd[cmd.index("--underresolved-solve-policy") + 1] == "warn"
    assert "--run-solves" in cmd
    assert "--mesh-only" not in cmd
    assert "--skip-missing-sources" in cmd
    assert "--notify" in cmd
    assert "--open-output-folder" in cmd
    assert "--open-report" not in cmd
    assert "--open-wg" not in cmd
    assert "--allow-underresolved-solve" not in cmd
    assert "--skip-per-driver-plots" not in cmd
    assert "--skip-combined-set" not in cmd
    assert "--skip-passive-cardioid-set" not in cmd
    assert "--skip-driver-lem-artifacts" not in cmd
    assert "--skip-derived-acoustics" not in cmd
    assert "--skip-radiation-impedance" not in cmd
    assert "--skip-pressure-bases" not in cmd
    assert "--no-run-report" not in cmd


def test_build_pipeline_command_forwards_output_skip_flags():
    helper = _load_helper()
    pipeline = _load_pipeline()

    cmd = _helper_command(
        helper,
        open_report=True,
        output_per_driver_plots=False,
        output_combined_set=False,
        output_passive_cardioid_set=False,
        output_driver_lem_artifacts=False,
        output_derived_acoustics=False,
        output_radiation_impedance=False,
        output_pressure_bases=False,
        output_run_report=False,
    )
    args = pipeline.parse_args(cmd[2:])

    assert "--open-report" in cmd
    expected = {
        "--skip-per-driver-plots",
        "--skip-combined-set",
        "--skip-passive-cardioid-set",
        "--skip-driver-lem-artifacts",
        "--skip-derived-acoustics",
        "--skip-radiation-impedance",
        "--skip-pressure-bases",
        "--no-run-report",
    }
    assert expected <= set(cmd)
    forwarded = {option for option, _attr in pipeline.OUTPUT_SKIP_FORWARD_OPTIONS}
    assert expected == forwarded
    assert args.open_report is True
    assert args.skip_per_driver_plots is True
    assert args.skip_combined_set is True
    assert args.skip_passive_cardioid_set is True
    assert args.skip_driver_lem_artifacts is True
    assert args.skip_derived_acoustics is True
    assert args.skip_radiation_impedance is True
    assert args.skip_pressure_bases is True
    assert args.no_run_report is True


def test_build_pipeline_command_rejects_vituixcad_crossover_without_combined():
    helper = _load_helper()

    with pytest.raises(ValueError, match="Combined/crossover"):
        _helper_command(
            helper,
            export_vituixcad=True,
            output_combined_set=False,
            crossover_mf_hf_hz="1000",
        )


def test_build_pipeline_command_can_request_plot_theme():
    helper = _load_helper()
    pipeline = _load_pipeline()

    cmd = _helper_command(helper, plot_theme="dark")
    args = pipeline.parse_args(cmd[2:])

    assert cmd[cmd.index("--plot-theme") + 1] == "dark"
    assert args.plot_theme == "dark"


def test_build_pipeline_command_can_request_passive_cardioid_combine():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        passive_cardioid_enabled=True,
        passive_cardioid_rear_volume_l="12.5",
        passive_cardioid_port_length_mm="18",
        passive_cardioid_port_area_cm2="100",
        passive_cardioid_foam_resistance_pa_s_m3="420",
        passive_cardioid_invert_port=False,
    )

    assert "--passive-cardioid-mf" in cmd
    assert cmd[cmd.index("--passive-cardioid-rear-volume-l") + 1] == "12.5"
    assert cmd[cmd.index("--passive-cardioid-port-length-mm") + 1] == "18"
    assert cmd[cmd.index("--passive-cardioid-port-area-cm2") + 1] == "100"
    assert cmd[cmd.index("--passive-cardioid-foam-resistance-pa-s-m3") + 1] == "420"
    assert "--no-passive-cardioid-invert-port" in cmd


def test_build_pipeline_command_maps_driver_lem_inputs():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        lf_driver_lem=(
            "Sd=320,Bl=11.6,Re=5.2,Le=0.8,Mmd=26.2,"
            "Cms=0.000252,Rms=3.18,Xmax=5.5"
        ),
        lf_driver_rear_volume_l="4.5",
        drive_voltage="4.0",
        rg_ohm="0.2",
    )

    driver_entries = [
        cmd[index + 1] for index, token in enumerate(cmd) if token == "--driver-lem"
    ]
    assert driver_entries == [
        "LF:Sd=320,Bl=11.6,Re=5.2,Le=0.8,Mmd=26.2,Cms=0.000252,Rms=3.18,Xmax=5.5,N=1"
    ]
    assert cmd[cmd.index("--driver-rear-volume-l") + 1] == "LF:4.5"
    assert cmd[cmd.index("--drive-voltage") + 1] == "4.0"
    assert cmd[cmd.index("--rg-ohm") + 1] == "0.2"


def test_build_pipeline_command_maps_passive_cardioid_coupled_driver_flags():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        passive_cardioid_enabled=True,
        passive_cardioid_coupled=True,
        passive_cardioid_driver_sd_cm2="132",
        passive_cardioid_driver_bl_tm="7.1",
        passive_cardioid_driver_re_ohm="5.8",
        passive_cardioid_driver_le_mh="0.18",
        passive_cardioid_driver_mms_g="14.2",
        passive_cardioid_driver_cms_mm_per_n="0.72",
        passive_cardioid_driver_vas_l="8.4",
        passive_cardioid_driver_fs_hz="72",
        passive_cardioid_driver_qms="3.9",
        passive_cardioid_drive_voltage="2.83",
        passive_cardioid_rg_ohm="0.2",
    )

    assert "--passive-cardioid-coupled" in cmd
    assert cmd[cmd.index("--passive-cardioid-driver-sd-cm2") + 1] == "132"
    assert cmd[cmd.index("--passive-cardioid-driver-bl-tm") + 1] == "7.1"
    assert cmd[cmd.index("--passive-cardioid-driver-re-ohm") + 1] == "5.8"
    assert cmd[cmd.index("--passive-cardioid-driver-le-mh") + 1] == "0.18"
    assert cmd[cmd.index("--passive-cardioid-driver-mms-g") + 1] == "14.2"
    assert cmd[cmd.index("--passive-cardioid-driver-cms-mm-per-n") + 1] == "0.72"
    assert cmd[cmd.index("--passive-cardioid-driver-vas-l") + 1] == "8.4"
    assert cmd[cmd.index("--passive-cardioid-driver-fs-hz") + 1] == "72"
    assert cmd[cmd.index("--passive-cardioid-driver-qms") + 1] == "3.9"
    assert cmd[cmd.index("--passive-cardioid-drive-voltage") + 1] == "2.83"
    assert cmd[cmd.index("--passive-cardioid-rg-ohm") + 1] == "0.2"


def test_build_pipeline_command_omits_blank_passive_cardioid_coupled_values():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        passive_cardioid_enabled=True,
        passive_cardioid_coupled=True,
        passive_cardioid_driver_sd_cm2=" ",
        passive_cardioid_driver_bl_tm="",
        passive_cardioid_driver_re_ohm=None,
        passive_cardioid_driver_le_mh=" ",
        passive_cardioid_driver_mms_g="",
        passive_cardioid_driver_cms_mm_per_n=" ",
        passive_cardioid_driver_vas_l="",
        passive_cardioid_driver_fs_hz=None,
        passive_cardioid_driver_qms=" ",
        passive_cardioid_drive_voltage="",
        passive_cardioid_rg_ohm=" ",
    )

    assert "--passive-cardioid-coupled" in cmd
    assert not any(flag.startswith("--passive-cardioid-driver-") for flag in cmd)
    assert "--passive-cardioid-drive-voltage" not in cmd
    assert "--passive-cardioid-rg-ohm" not in cmd


def test_build_pipeline_command_coupled_off_emits_no_driver_flags():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        passive_cardioid_enabled=True,
        passive_cardioid_coupled=False,
        passive_cardioid_driver_sd_cm2="132",
        passive_cardioid_driver_bl_tm="7.1",
        passive_cardioid_driver_re_ohm="5.8",
        passive_cardioid_driver_le_mh="0.18",
        passive_cardioid_driver_mms_g="14.2",
        passive_cardioid_driver_cms_mm_per_n="0.72",
        passive_cardioid_driver_qms="3.9",
        passive_cardioid_drive_voltage="2.83",
        passive_cardioid_rg_ohm="0.2",
    )

    assert "--passive-cardioid-mf" in cmd
    assert "--passive-cardioid-coupled" not in cmd
    assert not any(flag.startswith("--passive-cardioid-driver-") for flag in cmd)
    assert "--passive-cardioid-drive-voltage" not in cmd
    assert "--passive-cardioid-rg-ohm" not in cmd


def test_build_pipeline_command_can_request_crossover_alignment_report():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        crossover_lf_mf_hz="130",
        crossover_mf_hf_hz="1000",
    )

    assert cmd[cmd.index("--crossover-lf-mf-hz") + 1] == "130"
    assert cmd[cmd.index("--crossover-mf-hf-hz") + 1] == "1000"


def test_build_pipeline_command_forwards_single_crossover_for_two_way():
    helper = _load_helper()

    cmd = _helper_command(helper, crossover_mf_hf_hz="1000")

    assert "--crossover-lf-mf-hz" not in cmd
    assert cmd[cmd.index("--crossover-mf-hf-hz") + 1] == "1000"


def test_build_pipeline_command_can_request_clamped_solve_policy():
    helper = _load_helper()

    cmd = _helper_command(helper, underresolved_solve_policy="clamp-per-source")

    assert cmd[cmd.index("--underresolved-solve-policy") + 1] == "clamp-per-source"


def test_build_pipeline_command_can_request_standard_bem_formulation():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        bem_formulation="standard",
        complex_k_shift="0.0125",
    )

    assert cmd[cmd.index("--bem-formulation") + 1] == "standard"
    assert cmd[cmd.index("--complex-k-shift") + 1] == "0.0125"


def test_build_pipeline_command_explicit_mirror_plane_keeps_quadrants_and_axes():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        symmetry_planes="x0,y0",
        quadrants="1",
        mirror_axes="x,y",
    )

    assert cmd[cmd.index("--symmetry-planes") + 1] == "x0,y0"
    assert cmd[cmd.index("--quadrants") + 1] == "1"
    assert cmd[cmd.index("--mirror-axes") + 1] == "x,y"


def test_build_pipeline_command_mesh_only_disables_solves():
    helper = _load_helper()

    cmd = _helper_command(helper, mesh_only=True)

    assert "--mesh-only" in cmd
    assert "--run-solves" not in cmd


def test_build_pipeline_command_allow_underresolved_replaces_policy():
    helper = _load_helper()

    cmd = _helper_command(helper, allow_underresolved_solve=True)

    assert "--allow-underresolved-solve" in cmd
    assert "--underresolved-solve-policy" not in cmd


def test_build_pipeline_command_shows_mesh_valid_markers_by_default():
    helper = _load_helper()

    cmd = _helper_command(helper)

    assert "--no-mesh-valid-markers" not in cmd
    assert "--mesh-valid-markers" not in cmd


def test_build_pipeline_command_hides_mesh_valid_markers_when_disabled():
    helper = _load_helper()

    cmd = _helper_command(helper, show_mesh_valid_markers=False)

    assert "--no-mesh-valid-markers" in cmd


def test_build_pipeline_command_omits_blank_rigid_resolution():
    helper = _load_helper()

    cmd = _helper_command(helper, rigid_res_mm="")

    assert "--rigid-res-mm" not in cmd


def test_build_pipeline_command_forwards_refine():
    helper = _load_helper()

    cmd = _helper_command(
        helper,
        refine=["Rear:8mm", "Flare:4mm", " "],
    )

    assert "--mesh-sizing-mode" not in cmd
    assert "--radiating-epw" not in cmd
    assert "--shadow-epw" not in cmd
    assert "--throat-epw" not in cmd
    assert "--refine" in cmd
    refine_values = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--refine"]
    assert refine_values == ["Rear:8mm", "Flare:4mm"]


def test_build_pipeline_command_omits_removed_role_dials_and_refine_when_unset():
    helper = _load_helper()

    cmd = _helper_command(helper)

    assert "--mesh-sizing-mode" not in cmd
    assert "--radiating-epw" not in cmd
    assert "--shadow-epw" not in cmd
    assert "--throat-epw" not in cmd
    assert "--refine" not in cmd


def test_manual_mm_clamp_estimate_uses_explicit_mm_caps():
    helper = _load_helper()

    clamped = helper.estimate_clamped_solve_band(
        sources="HF:5",
        rigid_res_mm="30",
        freq_max_hz="20000",
    )

    assert clamped is not None
    assert clamped["HF"] == pytest.approx(343000.0 / (6.0 * 30.0), rel=1e-3)


def test_estimate_design_mesh_cost_sizes_by_role():
    helper = _load_helper()

    faces = [
        # HF source patch at origin
        {"area_mm2": 2000.0, "centroid": (0.0, 0.0, 0.0), "source_name": "HF"},
        # near-field wall close to the source
        {"area_mm2": 40000.0, "centroid": (30.0, 0.0, 0.0), "source_name": None},
        # far shadow wall well beyond the transition
        {"area_mm2": 200000.0, "centroid": (600.0, 0.0, 0.0), "source_name": None},
    ]
    estimate = helper.estimate_design_mesh_cost(
        faces,
        source_res_mm={"HF": 5.0},
        transition_mm=200.0,
        rigid_res_mm=30.0,
        freq_count=60,
    )
    assert estimate["n_triangles"] > 0
    assert "radiating" in estimate["per_role_triangles"]
    assert "near_field" in estimate["per_role_triangles"]
    # radiating validity, matrix RAM, and a feasibility flag are reported
    assert estimate["per_role_valid_f_max_hz"]["radiating"] >= 10000.0 - 200.0
    assert estimate["ram_gb"] > 0.0
    summary = helper.format_mesh_cost_summary(estimate)
    assert "triangles" in summary and "GB" in summary


def test_build_source_specs_uses_nonblank_lf_mf_hf_resolutions():
    helper = _load_helper()

    sources = helper.build_source_specs(
        lf_mesh_mm="20",
        mf_mesh_mm=" 10 ",
        hf_mesh_mm="5",
    )

    assert sources == "LF:20,MF:10,HF:5"


def test_build_source_specs_adds_port_exit_tags_when_requested():
    helper = _load_helper()

    sources = helper.build_source_specs(
        lf_mesh_mm="",
        mf_mesh_mm="",
        hf_mesh_mm="5",
        port_exit_l_mesh_mm=" 8 ",
        port_exit_r_mesh_mm="9",
    )

    assert sources == "HF:5,PORT_EXIT_L:8:10,PORT_EXIT_R:9:11"


def test_build_source_specs_adds_generic_port_exit_tag_when_requested():
    helper = _load_helper()

    sources = helper.build_source_specs(
        lf_mesh_mm="",
        mf_mesh_mm="",
        hf_mesh_mm="5",
        port_exit_mesh_mm=" 8 ",
    )

    assert sources == "HF:5,PORT_EXIT:8:10"


def test_build_source_specs_rejects_generic_and_side_port_exits_together():
    helper = _load_helper()

    with pytest.raises(ValueError, match="PORT_EXIT cannot be combined"):
        helper.build_source_specs(
            lf_mesh_mm="",
            mf_mesh_mm="",
            hf_mesh_mm="5",
            port_exit_mesh_mm="8",
            port_exit_l_mesh_mm="8",
        )


def test_named_preset_round_trip_through_launch_helper(tmp_path):
    helper = _load_helper()
    settings = {
        "settings_version": 13,
        "lf_mesh_mm": "18",
        "hf_mesh_mm": "4",
        "freq_count": "47",
        "output_run_report": False,
    }

    path = helper.save_preset("Sweep A", settings, presets_dir=tmp_path)
    loaded = helper.load_preset("Sweep_A", presets_dir=tmp_path)

    assert path == tmp_path / "Sweep_A.json"
    assert helper.list_preset_names(presets_dir=tmp_path) == ["Sweep_A"]
    assert loaded == settings


def test_pipeline_preset_defaults_keep_explicit_cli_precedence(tmp_path):
    pipeline = _load_pipeline()
    preset = tmp_path / "headless.json"
    preset.write_text(
        json.dumps(
            {
                "settings_version": 13,
                "lf_mesh_mm": "18",
                "mf_mesh_mm": "",
                "hf_mesh_mm": "4",
                "freq_min_hz": "80",
                "freq_max_hz": "12000",
                "freq_count": "31",
                "freq_spacing": "linear",
                "transition_mm": "160",
                "crossover_mf_hf_hz": "950",
                "clamp_to_mesh_limit": True,
                "show_mesh_valid_markers": False,
                "output_combined_set": False,
                "mesh_only": False,
            }
        ),
        encoding="utf-8",
    )

    args = pipeline.parse_args(
        [
            "--step",
            "/tmp/design.step",
            "--out",
            "/tmp/out",
            "--preset",
            str(preset),
            "--source",
            "MF:10",
            "--freq-count",
            "99",
            # Explicitly passed AT the argparse default: must still beat the
            # preset's 80 (guards against default-comparison merge regressions).
            "--freq-min-hz",
            "50",
        ]
    )

    assert args.source == ["MF:10"]
    assert args.sources == []
    assert args.freq_min_hz == pytest.approx(50.0)
    assert args.freq_max_hz == pytest.approx(12000.0)
    assert args.freq_count == 99
    assert args.freq_spacing == "linear"
    assert args.transition_mm == pytest.approx(160.0)
    assert args.crossover_mf_hf_hz == pytest.approx(950.0)
    assert args.underresolved_solve_policy == "clamp-per-source"
    assert args.mesh_valid_markers is False
    assert args.skip_combined_set is True
    assert args.run_solves is True
    assert args.mesh_only is False


def test_pipeline_normalizes_explicit_port_exit_source_tags():
    pipeline = _load_pipeline()

    assert pipeline._normalize_sources(["LF:30", "MF:20", "HF:4", "PORT_EXIT_L:25:10"]) == [
        "LF:30:2",
        "MF:20:3",
        "HF:4:4",
        "PORT_EXIT_L:25:10",
    ]


def test_pipeline_normalizes_generic_port_exit_to_canonical_tag():
    pipeline = _load_pipeline()

    assert pipeline._normalize_sources(["LF:30", "MF:20", "HF:4", "PORT_EXIT:25"]) == [
        "LF:30:2",
        "MF:20:3",
        "HF:4:4",
        "PORT_EXIT:25:10",
    ]


def test_pipeline_rejects_actual_duplicate_explicit_source_tags():
    pipeline = _load_pipeline()

    with pytest.raises(ValueError, match="duplicate source tag 10"):
        pipeline._normalize_sources(["PORT_EXIT_L:25:10", "PORT_EXIT_R:25:10"])


def test_build_source_specs_omits_blank_resolutions():
    helper = _load_helper()

    sources = helper.build_source_specs(
        lf_mesh_mm="",
        mf_mesh_mm=" ",
        hf_mesh_mm="5",
    )

    assert sources == "HF:5"


def test_mirror_plane_helpers_map_fusion_names_to_solver_planes():
    helper = _load_helper()

    assert helper.symmetry_planes_for_mirror_plane("Auto detect") == "auto"
    assert helper.mirror_axes_for_symmetry_planes("auto") == "auto"
    assert helper.quadrants_for_symmetry_planes("auto") == "auto"
    assert helper.symmetry_planes_for_mirror_plane("Top/Bottom") == "z0"
    assert helper.mirror_axes_for_symmetry_planes("z0") == "z"
    assert helper.quadrants_for_symmetry_planes("z0") == "1234"
    assert helper.symmetry_planes_for_mirror_plane("Left/Right + Front/Back") == "x0,y0"
    assert helper.mirror_axes_for_symmetry_planes("x0,y0") == "x,y"
    assert helper.quadrants_for_symmetry_planes("x0,y0") == "1"


def test_pipeline_maps_z0_to_native_xy_symmetry():
    pipeline = _load_pipeline()

    assert pipeline._parse_symmetry_planes("top-bottom", quadrants=1) == ("z0",)
    assert pipeline._native_symmetry_for_planes(("z0",)) == "xy"
    args = pipeline.parse_args(
        [
            "--step",
            "/tmp/design.step",
            "--out",
            "/tmp/out",
            "--source",
            "HF:5:4",
            "--symmetry-planes",
            "z0",
            "--native-symmetry-plane",
            "xy",
        ]
    )
    assert args.native_symmetry_plane == "xy"
    # Defaults to the strict guard; opt-out parses for open-mouth horns.
    assert args.native_check_open_edges is True
    relaxed = pipeline.parse_args(
        [
            "--step",
            "/tmp/design.step",
            "--out",
            "/tmp/out",
            "--source",
            "HF:5:4",
            "--symmetry-planes",
            "z0",
            "--native-symmetry-plane",
            "xy",
            "--no-native-check-open-edges",
        ]
    )
    assert relaxed.native_check_open_edges is False


def test_pipeline_filters_sources_to_prepare_manifest():
    pipeline = _load_pipeline()

    sources = ["LF:20:2", "MF:15:3", "HF:5:4"]
    manifest = {
        "sources": {
            "LF": {"tag": 2},
            "HF": {"tag": 4},
        },
        "skipped_sources": {
            "MF": {"tag": 3},
        },
    }

    assert pipeline._sources_present_in_manifest(sources, manifest) == [
        "LF:20:2",
        "HF:5:4",
    ]


DEFAULT_FRAME_INFERENCE = [
    {
        "name": "HF",
        "triangles": 23,
        "inferred_forward_axis": [0.0, 0.0, 1.0],
        "mouth_center_for_inferred_axis": [75.0, 150.0, 310.0],
    }
]


def _fake_run_logged(
    calls,
    *,
    prep_manifest_payload,
    solve_manifest_payload=None,
    frame_inference=None,
):
    def fake_run_logged(cmd, *, cwd, stdout_path, stderr_path):
        script_name = Path(cmd[1]).name
        calls.append((script_name, cmd))
        out_dir = Path(cmd[cmd.index("--out") + 1])
        if script_name == "prepare_step_for_wg_metal.py":
            (out_dir / "manifest.json").write_text(
                json.dumps(prep_manifest_payload) + "\n",
                encoding="utf-8",
            )
        elif script_name == "diagnose_wg_metal_orientation.py":
            (out_dir / "orientation_report.json").write_text(
                json.dumps(
                    {
                        "expanded_mesh": {},
                        "expanded_4quarter": {},
                        "source_frame_inference": (
                            DEFAULT_FRAME_INFERENCE
                            if frame_inference is None
                            else frame_inference
                        ),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script_name == "solve_fusion_wg_metal.py":
            payload = solve_manifest_payload or {"status": "complete"}
            _run_manifest_path(out_dir, "direct_solve_manifest.json").write_text(
                json.dumps(payload) + "\n",
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"unexpected command: {script_name}")
        return 0

    return fake_run_logged


def _option_values(cmd: list[str], option: str) -> list[str]:
    return [
        cmd[index + 1]
        for index, token in enumerate(cmd[:-1])
        if token == option
    ]


def _underresolved_prep_manifest():
    return {
        "sources": {"LF": {"tag": 2}, "HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0", "y0"],
        "mesh_frequency_validation": {
            "status": "invalid",
            "requested_max_frequency_hz": 20_000.0,
            "max_valid_frequency_hz": 1_475.0,
            "per_source": {
                "LF": {"max_valid_frequency_hz": 1_475.0, "status": "invalid"},
                "HF": {"max_valid_frequency_hz": 9_767.0, "status": "invalid"},
            },
            "warnings": ["underresolved"],
        },
    }


def test_pipeline_orders_direct_solve_sources_hf_mf_lf(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {
            "LF": {"tag": 2},
            "MF": {"tag": 3},
            "HF": {"tag": 4},
            "PORT_EXIT": {"tag": 10},
        },
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0", "y0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "LF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
                "MF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
                "HF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
                "PORT_EXIT": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=prep_manifest),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:20",
            "--source",
            "PORT_EXIT:8",
            "--source",
            "MF:10",
            "--source",
            "HF:5",
            "--freq-max-hz",
            "2000",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert _option_values(solve_cmd, "--source") == [
        "HF:5:4",
        "MF:10:3",
        "LF:20:2",
        "PORT_EXIT:8:10",
    ]


def test_pipeline_forwards_output_skip_flags_to_direct_solve(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0", "y0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=prep_manifest),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--freq-max-hz",
            "2000",
            "--run-solves",
            "--skip-per-driver-plots",
            "--skip-combined-set",
            "--skip-passive-cardioid-set",
            "--skip-driver-lem-artifacts",
            "--skip-derived-acoustics",
            "--skip-radiation-impedance",
            "--skip-pressure-bases",
            "--no-run-report",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    for option, _attr in pipeline.OUTPUT_SKIP_FORWARD_OPTIONS:
        assert option in solve_cmd


def test_pipeline_forwards_plot_theme_to_direct_solve(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=prep_manifest),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--freq-max-hz",
            "2000",
            "--plot-theme",
            "dark",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--plot-theme") + 1] == "dark"


def test_pipeline_writes_solving_manifest_before_direct_solve_returns(
    tmp_path,
    monkeypatch,
):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }

    def fake_run_logged(cmd, *, cwd, stdout_path, stderr_path):
        script_name = Path(cmd[1]).name
        calls.append((script_name, cmd))
        out_dir = Path(cmd[cmd.index("--out") + 1])
        if script_name == "prepare_step_for_wg_metal.py":
            (out_dir / "manifest.json").write_text(
                json.dumps(prep_manifest) + "\n",
                encoding="utf-8",
            )
        elif script_name == "diagnose_wg_metal_orientation.py":
            (out_dir / "orientation_report.json").write_text(
                json.dumps(
                    {
                        "expanded_mesh": {},
                        "expanded_4quarter": {},
                        "source_frame_inference": DEFAULT_FRAME_INFERENCE,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script_name == "solve_fusion_wg_metal.py":
            manifest = json.loads(
                _run_manifest_path(out_dir, "fusion_wg_pipeline_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["status"] == "solving"
            assert manifest["commands"]["solve"] == cmd
            assert manifest["solve_sources"] == ["HF:5:4"]
            _run_manifest_path(out_dir, "direct_solve_manifest.json").write_text(
                json.dumps({"status": "complete"}) + "\n",
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"unexpected command: {script_name}")
        return 0

    monkeypatch.setattr(pipeline, "_run_logged", fake_run_logged)

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--freq-max-hz",
            "2000",
            "--run-solves",
        ]
    )

    assert rc == 0


def test_pipeline_opens_output_folder_after_solve_failure(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    out_dir = tmp_path / "out"
    opened = []
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }

    def fake_run_logged(cmd, *, cwd, stdout_path, stderr_path):
        script_name = Path(cmd[1]).name
        command_out = Path(cmd[cmd.index("--out") + 1])
        if script_name == "prepare_step_for_wg_metal.py":
            (command_out / "manifest.json").write_text(
                json.dumps(prep_manifest) + "\n",
                encoding="utf-8",
            )
            return 0
        if script_name == "diagnose_wg_metal_orientation.py":
            (command_out / "orientation_report.json").write_text(
                json.dumps(
                    {
                        "expanded_mesh": {},
                        "expanded_4quarter": {},
                        "source_frame_inference": DEFAULT_FRAME_INFERENCE,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return 0
        if script_name == "solve_fusion_wg_metal.py":
            return 7
        raise AssertionError(f"unexpected command: {script_name}")

    monkeypatch.setattr(pipeline, "_run_logged", fake_run_logged)
    monkeypatch.setattr(
        pipeline,
        "_open_output_folder",
        lambda path: opened.append(Path(path)),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(out_dir),
            "--source",
            "HF:5",
            "--freq-max-hz",
            "2000",
            "--run-solves",
            "--open-output-folder",
        ]
    )

    assert rc == 7
    assert opened == [out_dir]


def test_pipeline_forwards_passive_cardioid_options_to_direct_solve(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"MF": {"tag": 3}, "PORT_EXIT": {"tag": 10}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 2000.0,
            "max_valid_frequency_hz": 3000.0,
            "per_source": {
                "MF": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
                "PORT_EXIT": {"max_valid_frequency_hz": 3000.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=prep_manifest),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "MF:20",
            "--source",
            "PORT_EXIT:25",
            "--freq-max-hz",
            "2000",
            "--run-solves",
            "--passive-cardioid-mf",
            "--passive-cardioid-rear-volume-l",
            "9.5",
            "--passive-cardioid-port-length-mm",
            "22",
            "--passive-cardioid-foam-resistance-pa-s-m3",
            "600",
            "--no-passive-cardioid-invert-port",
            "--driver-lem",
            "MF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3",
            "--driver-rear-volume-l",
            "MF:4.5",
            "--drive-voltage",
            "4.0",
            "--rg-ohm",
            "0.2",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert "--passive-cardioid-mf" in solve_cmd
    assert solve_cmd[solve_cmd.index("--passive-cardioid-rear-volume-l") + 1] == "9.5"
    assert solve_cmd[solve_cmd.index("--passive-cardioid-port-length-mm") + 1] == "22.0"
    assert solve_cmd[solve_cmd.index("--passive-cardioid-foam-resistance-pa-s-m3") + 1] == "600.0"
    assert "--no-passive-cardioid-invert-port" in solve_cmd
    assert solve_cmd[solve_cmd.index("--driver-lem") + 1] == (
        "MF:Sd=200,Bl=8,Re=5.5,Mmd=18,Cms=6e-4,Rms=2.3"
    )
    assert solve_cmd[solve_cmd.index("--driver-rear-volume-l") + 1] == "MF:4.5"
    assert solve_cmd[solve_cmd.index("--drive-voltage") + 1] == "4.0"
    assert solve_cmd[solve_cmd.index("--rg-ohm") + 1] == "0.2"


def test_pipeline_default_warns_and_solves_underresolved_sources(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
        ]
    )

    assert rc == 0
    assert [name for name, _ in calls] == [
        "prepare_step_for_wg_metal.py",
        "diagnose_wg_metal_orientation.py",
        "solve_fusion_wg_metal.py",
    ]
    prep_cmd = calls[0][1]
    assert "--mesh-sizing-mode" not in prep_cmd
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--freq-max-hz") + 1] == "20000.0"
    assert solve_cmd[solve_cmd.index("--bem-formulation") + 1] == "complex_k"
    assert solve_cmd[solve_cmd.index("--complex-k-shift") + 1] == "0.005"
    assert "--source-freq-max" not in solve_cmd
    # The open-edge guard choice is forwarded to the metal solve (default strict).
    assert "--native-check-open-edges" in solve_cmd
    assert "--no-native-check-open-edges" not in solve_cmd
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "complete"
    assert manifest["underresolved_solve_policy"] == "warn"
    adjustment = manifest["solve_frequency_adjustment"]
    assert adjustment["policy"] == "warn"
    assert adjustment["mesh_valid_freq_max_hz"] == {"LF": 1475.0, "HF": 9767.0}


def test_pipeline_overlays_mesh_valid_markers_by_default(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    overlays = {
        solve_cmd[i + 1]
        for i, token in enumerate(solve_cmd)
        if token == "--source-mesh-valid-hz"
    }
    assert overlays == {"LF:1475.0", "HF:9767.0"}
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    adjustment = manifest["solve_frequency_adjustment"]
    assert adjustment["mesh_valid_overlay_freq_max_hz"] == {"LF": 1475.0, "HF": 9767.0}


def test_pipeline_hides_mesh_valid_markers_when_disabled(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
            "--no-mesh-valid-markers",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert "--source-mesh-valid-hz" not in solve_cmd
    assert "--source-aperture-valid-hz" not in solve_cmd
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    adjustment = manifest["solve_frequency_adjustment"]
    # The overlay is gone but the authoritative warn-policy record remains.
    assert "mesh_valid_overlay_freq_max_hz" not in adjustment
    assert adjustment["mesh_valid_freq_max_hz"] == {"LF": 1475.0, "HF": 9767.0}


def test_pipeline_complex_k_dash_alias_is_forwarded_canonicalized(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--freq-min-hz",
            "10",
            "--bem-formulation",
            "complex-k",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--bem-formulation") + 1] == "complex_k"


def test_pipeline_forwards_manual_mm_mode_to_prepare(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--run-solves",
        ]
    )

    assert rc == 0
    prep_cmd = calls[0][1]
    assert "--mesh-sizing-mode" not in prep_cmd


def test_pipeline_clamp_policy_clamps_each_underresolved_source_separately(
    tmp_path, monkeypatch
):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
            "--underresolved-solve-policy",
            "clamp-per-source",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    limits = {
        solve_cmd[i + 1]
        for i, token in enumerate(solve_cmd)
        if token == "--source-freq-max"
    }
    assert limits == {"LF:1475.0", "HF:9767.0"}
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["underresolved_solve_policy"] == "clamp-per-source"
    adjustment = manifest["solve_frequency_adjustment"]
    assert adjustment["policy"] == "clamp-per-source"
    assert adjustment["per_source_freq_max_hz"] == {"LF": 1475.0, "HF": 9767.0}
    assert adjustment["skipped_sources"] == []


def test_pipeline_default_skips_sources_below_freq_min(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "2000",
            "--run-solves",
            "--underresolved-solve-policy",
            "clamp-per-source",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert "LF:30:2" not in solve_cmd
    assert "HF:5:4" in solve_cmd
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    skipped = manifest["solve_frequency_adjustment"]["skipped_sources"]
    assert [item["name"] for item in skipped] == ["LF"]


def test_pipeline_fails_when_no_source_can_solve_above_freq_min(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "15000",
            "--run-solves",
            "--underresolved-solve-policy",
            "clamp-per-source",
        ]
    )

    assert rc == 2
    assert [name for name, _ in calls] == [
        "prepare_step_for_wg_metal.py",
        "diagnose_wg_metal_orientation.py",
    ]
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "failed"


def test_pipeline_strict_fail_policy_still_refuses_underresolved_solve(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--run-solves",
            "--underresolved-solve-policy",
            "fail",
        ]
    )

    assert rc == 2
    assert [name for name, _ in calls] == [
        "prepare_step_for_wg_metal.py",
        "diagnose_wg_metal_orientation.py",
    ]
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "failed"
    assert manifest["mesh_frequency_validation"]["status"] == "invalid"


def test_pipeline_warns_and_solves_underresolved_when_policy_is_warn(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--run-solves",
            "--underresolved-solve-policy",
            "warn",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--freq-max-hz") + 1] == "20000.0"
    assert "--source-freq-max" not in solve_cmd
    # The open-edge guard choice is forwarded to the metal solve (default strict).
    assert "--native-check-open-edges" in solve_cmd
    assert "--no-native-check-open-edges" not in solve_cmd
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "complete"
    assert manifest["underresolved_solve_policy"] == "warn"
    assert manifest["solve_frequency_adjustment"]["policy"] == "warn"
    # The mesh-valid ceilings stay visible so plots can be read accordingly.
    assert manifest["solve_frequency_adjustment"]["mesh_valid_freq_max_hz"] == {
        "HF": 9_767.0,
    }


def test_pipeline_auto_symmetry_uses_planes_detected_by_prepare(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "symmetry_planes_mode": "auto",
        "topology": {
            "symmetry_plane_detection": {
                "detected_planes": ["x0"],
                "plane_free_edge_counts": {"x0": 40, "y0": 0, "z0": 0},
            }
        },
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 8_000.0,
            "max_valid_frequency_hz": 9_767.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 9_767.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=prep_manifest),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--symmetry-planes",
            "auto",
            "--freq-max-hz",
            "8000",
            "--run-solves",
        ]
    )

    assert rc == 0
    prep_cmd = calls[0][1]
    assert prep_cmd[prep_cmd.index("--symmetry-planes") + 1] == "auto"
    diagnose_cmd = calls[1][1]
    assert diagnose_cmd[diagnose_cmd.index("--mirror-axes") + 1] == "x"
    assert diagnose_cmd[diagnose_cmd.index("--tol") + 1] == "1e-05"
    assert diagnose_cmd[diagnose_cmd.index("--unit-scale-to-m") + 1] == "0.001"
    solve_cmd = calls[2][1]
    assert solve_cmd[solve_cmd.index("--native-symmetry-plane") + 1] == "yz"
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["symmetry_planes"] == ["x0"]
    assert manifest["symmetry_planes_mode"] == "auto"
    assert manifest["quadrants"] == 14


def test_pipeline_wg_handoff_uses_expanded_full_domain_meshes(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    expanded_mesh = str(tmp_path / "out" / "expanded_2q_x.msh")
    expanded_source = str(tmp_path / "out" / "HF_source_tag2_m.msh")
    prep_manifest = {
        "sources": {"HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {"HF": "reduced_HF_source_tag2_m.msh"},
        "solver_ready": True,
        "symmetry_planes": ["x0"],
        "symmetry_planes_mode": "auto",
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 8_000.0,
            "max_valid_frequency_hz": 9_767.0,
            "per_source": {
                "HF": {"max_valid_frequency_hz": 9_767.0, "status": "valid"},
            },
            "warnings": [],
        },
    }

    def fake_run_logged(cmd, *, cwd, stdout_path, stderr_path):
        script_name = Path(cmd[1]).name
        calls.append((script_name, cmd))
        out_dir = Path(cmd[cmd.index("--out") + 1])
        if script_name == "prepare_step_for_wg_metal.py":
            (out_dir / "manifest.json").write_text(
                json.dumps(prep_manifest) + "\n",
                encoding="utf-8",
            )
        elif script_name == "diagnose_wg_metal_orientation.py":
            (out_dir / "orientation_report.json").write_text(
                json.dumps(
                    {
                        "expanded_mesh": {"mesh": expanded_mesh},
                        "expanded_4quarter": {},
                        "wg_source_meshes_m": {"HF": expanded_source},
                        "source_frame_inference": DEFAULT_FRAME_INFERENCE,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script_name == "solve_fusion_wg_metal.py":
            _run_manifest_path(out_dir, "direct_solve_manifest.json").write_text(
                json.dumps({"status": "complete"}) + "\n",
                encoding="utf-8",
            )
        return 0

    monkeypatch.setattr(pipeline, "_run_logged", fake_run_logged)

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--symmetry-planes",
            "auto",
            "--freq-max-hz",
            "8000",
            "--run-solves",
        ]
    )

    assert rc == 0
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["solve_mesh"].endswith("tagged_sources.msh")
    assert manifest["waveguide_generator"]["import_mesh"] == expanded_mesh
    assert manifest["waveguide_generator"]["per_source_meshes_m"] == {
        "HF": expanded_source
    }
    assert manifest["wg_source_meshes_m"] == {"HF": expanded_source}


def test_pipeline_auto_frame_follows_inferred_horn_axis(tmp_path, monkeypatch):
    """EPAL_HORN_v22 regression: a z0 half model firing along +X must not be
    observed with the legacy hardcoded +Z frame."""
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"LF": {"tag": 2}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": ["z0"],
        "symmetry_planes_mode": "auto",
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 700.0,
            "max_valid_frequency_hz": 714.0,
            "per_source": {
                "LF": {"max_valid_frequency_hz": 714.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    inference = [
        {
            "name": "LF",
            "triangles": 173,
            "inferred_forward_axis": [0.997, 0.083, 0.0],
            "mouth_center_for_inferred_axis": [814.0, 1133.0, 149.0],
        }
    ]
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(
            calls,
            prep_manifest_payload=prep_manifest,
            frame_inference=inference,
        ),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:80",
            "--symmetry-planes",
            "auto",
            "--freq-max-hz",
            "700",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--frame-axis") + 1] == "1,0,0"
    assert solve_cmd[solve_cmd.index("--frame-u") + 1] == "0,1,0"
    assert solve_cmd[solve_cmd.index("--frame-v") + 1] == "0,0,1"
    assert solve_cmd[solve_cmd.index("--frame-origin") + 1] == "0.814,1.133,0"
    assert solve_cmd[solve_cmd.index("--native-symmetry-plane") + 1] == "xy"
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    frame = manifest["observation_frame"]
    assert frame["mode"] == "auto"
    assert frame["axis"] == [1.0, 0.0, 0.0]
    assert frame["warnings"] == []


def test_pipeline_auto_frame_constrains_axis_to_cut_planes_for_meh(tmp_path, monkeypatch):
    """MEH side-mounted drivers fire into the horn (LF tilted, MF along -y).
    The cut planes x0+y0 force the radiation axis to z regardless of the
    side-driver normals."""
    pipeline = _load_pipeline()
    calls = []
    inference = [
        {
            "name": "LF",
            "triangles": 173,
            "inferred_forward_axis": [-0.639, 0.0, 0.769],
            "mouth_center_for_inferred_axis": [0.0, 210.8, 296.8],
        },
        {
            "name": "MF",
            "triangles": 243,
            "inferred_forward_axis": [0.0, -0.907, 0.422],
            "mouth_center_for_inferred_axis": [353.5, 0.0, 293.5],
        },
        {
            "name": "HF",
            "triangles": 23,
            "inferred_forward_axis": [0.0, 0.0, 1.0],
            "mouth_center_for_inferred_axis": [241.4, 164.9, 297.1],
        },
    ]

    def fake_run_logged(cmd, *, cwd, stdout_path, stderr_path):
        script_name = Path(cmd[1]).name
        calls.append((script_name, cmd))
        out_dir = Path(cmd[cmd.index("--out") + 1])
        if script_name == "prepare_step_for_wg_metal.py":
            (out_dir / "manifest.json").write_text(
                json.dumps(_underresolved_prep_manifest()) + "\n", encoding="utf-8"
            )
        elif script_name == "diagnose_wg_metal_orientation.py":
            (out_dir / "orientation_report.json").write_text(
                json.dumps(
                    {
                        "expanded_mesh": {},
                        "expanded_4quarter": {},
                        "source_frame_inference": inference,
                        "principal_axis_mouth_centers": {
                            "+z": [188.0, 112.0, 298.0],
                            "-z": [180.0, 100.0, -10.0],
                            "+x": [376.0, 110.0, 100.0],
                            "-x": [0.0, 110.0, 100.0],
                            "+y": [180.0, 225.0, 100.0],
                            "-y": [180.0, 0.0, 100.0],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script_name == "solve_fusion_wg_metal.py":
            _run_manifest_path(out_dir, "direct_solve_manifest.json").write_text(
                json.dumps({"status": "complete"}) + "\n", encoding="utf-8"
            )
        return 0

    monkeypatch.setattr(pipeline, "_run_logged", fake_run_logged)

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--frame-axis") + 1] == "0,0,1"
    assert solve_cmd[solve_cmd.index("--frame-origin") + 1] == "0,0,0.298"
    manifest = json.loads(
        _run_manifest_path(tmp_path / "out", "fusion_wg_pipeline_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    frame = manifest["observation_frame"]
    assert frame["axis"] == [0.0, 0.0, 1.0]
    assert frame["axis_candidates"] == ["z"]
    assert frame["warnings"] == []
    assert frame["mouth_center_source"] == "principal_axis"


def test_pipeline_auto_frame_passes_negative_origin_as_option_value(
    tmp_path, monkeypatch
):
    pipeline = _load_pipeline()
    calls = []
    prep_manifest = {
        "sources": {"LF": {"tag": 2}, "HF": {"tag": 4}},
        "skipped_sources": {},
        "tagged_mesh_step_units": "tagged_sources.msh",
        "wg_source_meshes_m": {},
        "solver_ready": True,
        "symmetry_planes": [],
        "symmetry_planes_mode": "auto",
        "mesh_frequency_validation": {
            "status": "valid",
            "requested_max_frequency_hz": 800.0,
            "max_valid_frequency_hz": 900.0,
            "per_source": {
                "LF": {"max_valid_frequency_hz": 900.0, "status": "valid"},
                "HF": {"max_valid_frequency_hz": 900.0, "status": "valid"},
            },
            "warnings": [],
        },
    }
    inference = [
        {
            "name": "LF",
            "triangles": 119,
            "inferred_forward_axis": [0.069, 0.0, 0.998],
            "mouth_center_for_inferred_axis": [-139.0, -257.04, -0.998632],
        },
        {
            "name": "HF",
            "triangles": 51,
            "inferred_forward_axis": [0.0, 0.0, 1.0],
            "mouth_center_for_inferred_axis": [-139.0, -257.04, -0.998632],
        },
    ]
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(
            calls,
            prep_manifest_payload=prep_manifest,
            frame_inference=inference,
        ),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "LF:30",
            "--source",
            "HF:4",
            "--symmetry-planes",
            "auto",
            "--freq-max-hz",
            "800",
            "--run-solves",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert "--frame-origin" not in solve_cmd
    assert "--frame-origin=-0.139,-0.25704,-0.000998632" in solve_cmd


def test_pipeline_explicit_frame_bypasses_auto(tmp_path, monkeypatch):
    pipeline = _load_pipeline()
    calls = []
    monkeypatch.setattr(
        pipeline,
        "_run_logged",
        _fake_run_logged(calls, prep_manifest_payload=_underresolved_prep_manifest()),
    )

    rc = pipeline.main(
        [
            "--step",
            str(tmp_path / "design.step"),
            "--out",
            str(tmp_path / "out"),
            "--source",
            "HF:5",
            "--freq-min-hz",
            "10",
            "--run-solves",
            "--frame-axis",
            "+Z",
            "--frame-origin",
            "0,0,0.42",
        ]
    )

    assert rc == 0
    solve_cmd = calls[-1][1]
    assert solve_cmd[solve_cmd.index("--frame-axis") + 1] == "+Z"
    assert solve_cmd[solve_cmd.index("--frame-origin") + 1] == "0,0,0.42"


def test_launch_metadata_lists_expected_logs_and_manifests(tmp_path):
    helper = _load_helper()

    metadata = helper.build_launch_metadata(
        command=["python", "pipeline.py"],
        pid=12345,
        started_at="2026-06-09T12:00:00",
        output_dir=tmp_path,
        step_path=tmp_path / "design.step",
        cwd=ROOT,
        status="running",
        fusion_archive_path=tmp_path / "design.f3d",
    )

    assert metadata["pid"] == 12345
    assert metadata["status"] == "running"
    assert metadata["step"] == str(tmp_path / "design.step")
    assert metadata["fusion_archive"] == str(tmp_path / "design.f3d")
    assert metadata["expected_paths"]["prepare_stdout"].endswith(
        "logs/prepare_step_for_wg_metal.stdout.log"
    )
    assert metadata["expected_paths"]["diagnose_stderr"].endswith(
        "logs/diagnose_wg_metal_orientation.stderr.log"
    )
    assert metadata["expected_paths"]["solve_stdout"].endswith(
        "logs/solve_fusion_wg_metal.stdout.log"
    )
    assert metadata["expected_paths"]["launch_metadata"].endswith(
        "manifests/fusion_addin_launch.json"
    )
    assert metadata["expected_paths"]["pipeline_manifest"].endswith(
        "manifests/fusion_wg_pipeline_manifest.json"
    )
    assert metadata["expected_paths"]["final_summary_manifest"].endswith(
        "manifests/final_summary_manifest.json"
    )
    assert metadata["expected_paths"]["direct_solve_manifest"].endswith(
        "manifests/direct_solve_manifest.json"
    )
    assert metadata["expected_paths"]["prepare_manifest"].endswith("mesh/manifest.json")
    assert metadata["expected_paths"]["tagged_sources_msh"].endswith(
        "mesh/tagged_sources.msh"
    )
    assert metadata["expected_paths"]["orientation_report"].endswith(
        "mesh/orientation_report.json"
    )
    assert metadata["expected_paths"]["combined_time_aligned_frequency_response_png"].endswith(
        "combined/combined_frequency_response_time_aligned.png"
    )
    assert metadata["expected_paths"]["driver_time_alignment_txt"].endswith(
        "combined/driver_time_alignment.txt"
    )
    assert metadata["expected_paths"]["port_exit_radiation_impedance_npz"].endswith(
        "sources/port_exit_radiation_impedance_matrix.npz"
    )
    assert metadata["expected_paths"]["run_report_html"].endswith("report.html")
    json.dumps(metadata)


def test_build_pipeline_command_can_request_vituixcad_export():
    helper = _load_helper()

    assert "--export-vituixcad" not in _helper_command(helper)
    cmd = _helper_command(helper, export_vituixcad=True)
    assert "--export-vituixcad" in cmd
